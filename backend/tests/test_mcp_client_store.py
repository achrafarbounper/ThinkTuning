# project/tests/test_mcp_client_store.py

"""Tests du registre MCP (MCPClientStore) — CRUD + révocation + métriques.

Valide le contrat du store de clients MCP (S4, tâche 10) :
- inscription d'un client avec son scope de sécurité
- listage des clients (sans exposition du secret)
- révocation avec motif (double révocation refusée, client inconnu)
- métriques d'usage (call_count, error_rate, scope_usage)
- authentification par secret hashé (fail-closed si révoqué)
- isolation des métriques entre clients
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.domain.ports.mcp_ports import MCPSecurityScope
from core.mcp_client_store import (
    MCPClientAlreadyExistsError,
    MCPClientNotFoundError,
    MCPClientRevokedError,
    MCPClientStore,
)


def _make_scope(
    client_id: str = "test-client",
    tenant_id: str = "default",
    role: str = "read_only",
    visible_tools: list[str] | None = None,
    visible_resources: list[str] | None = None,
    visible_prompts: list[str] | None = None,
    sampling_enabled: bool = False,
    rate_limit_per_minute: int = 60,
    destructive_quota: int = 5,
) -> MCPSecurityScope:
    """Fabrique un MCPSecurityScope de test avec des valeurs par défaut sûres."""
    return MCPSecurityScope(
        client_id=client_id,
        tenant_id=tenant_id,
        role=role,
        visible_tools=visible_tools or [],
        visible_resources=visible_resources or [],
        visible_prompts=visible_prompts or [],
        sampling_enabled=sampling_enabled,
        rate_limit_per_minute=rate_limit_per_minute,
        destructive_quota=destructive_quota,
        revoked=False,
        revoked_at=None,
        revoked_reason="",
    )


@pytest.fixture()
def store(tmp_path):
    """MCPClientStore sur une base SQLite temporaire (isolée par test)."""
    return MCPClientStore(path=str(tmp_path / "mcp_clients.db"))


@pytest.fixture()
def registered_client(store):
    """Client 'read_only' inscrit, prêt pour les tests CRUD / révocation."""
    return store.register(
        client_id="cli-001",
        secret="secret-secret-1234",
        scope=_make_scope(client_id="cli-001", role="read_only"),
    )


@pytest.fixture()
def admin_client(store):
    """Client 'admin' inscrit (sampling + quota + whitelist tools)."""
    return store.register(
        client_id="cli-admin",
        secret="admin-secret-5678",
        scope=_make_scope(
            client_id="cli-admin",
            role="admin",
            sampling_enabled=True,
            rate_limit_per_minute=600,
            destructive_quota=20,
            visible_tools=["tools/list", "tools/call", "resources/list"],
        ),
    )


# ============================================================================
# Registration
# ============================================================================


def test_register_creates_client_with_scope_fields(store):
    """register persiste tous les champs du scope et masque le secret."""
    result = store.register(
        client_id="cli-test-01",
        secret="my-secret",
        scope=_make_scope(
            client_id="cli-test-01",
            tenant_id="staging",
            role="contributor",
            visible_tools=["tool_a", "tool_b"],
            sampling_enabled=True,
            rate_limit_per_minute=120,
            destructive_quota=10,
        ),
    )

    assert result["client_id"] == "cli-test-01"
    assert result["tenant_id"] == "staging"
    assert result["role"] == "contributor"
    assert result["visible_tools"] == ["tool_a", "tool_b"]
    assert result["sampling_enabled"] is True
    assert result["rate_limit_per_minute"] == 120
    assert result["destructive_quota"] == 10
    assert result["revoked"] is False
    assert result["revoked_at"] is None
    assert result["revoked_reason"] == ""
    assert result["call_count"] == 0
    assert result["error_count"] == 0
    assert result["scope_usage"] == {}
    assert "secret_hash" not in result


def test_register_rejects_blank_client_id(store):
    """Un client_id vide / blanc est rejeté (ValueError)."""
    with pytest.raises(ValueError, match="client_id"):
        store.register(client_id="   ", secret="s", scope=_make_scope("cli-x"))


def test_register_rejects_blank_secret(store):
    """Un secret vide / blanc est rejeté (ValueError)."""
    with pytest.raises(ValueError, match="secret"):
        store.register(client_id="cli-ok", secret="   ", scope=_make_scope("cli-ok"))


def test_register_rejects_duplicate_client_id(store):
    """Deux inscriptions avec le même client_id → MCPClientAlreadyExistsError."""
    store.register("dup-001", "s1", _make_scope("dup-001"))
    with pytest.raises(MCPClientAlreadyExistsError):
        store.register("dup-001", "s2", _make_scope("dup-001"))


def test_register_persists_across_store_instances(tmp_path):
    """Le client inscrit est retrouvé après réouverture de la base."""
    path = str(tmp_path / "persist.db")
    store1 = MCPClientStore(path=path)
    store1.register("persist-01", "secret-x", _make_scope("persist-01"))

    store2 = MCPClientStore(path=path)
    clients = store2.list()
    assert [c["client_id"] for c in clients] == ["persist-01"]


# ============================================================================
# List
# ============================================================================


def test_list_returns_empty_when_no_clients(store):
    """list() renvoie [] quand aucun client n'est inscrit."""
    assert store.list() == []


def test_list_returns_all_clients_newest_first(store, registered_client, admin_client):
    """list() renvoie tous les clients, du plus récent au plus ancien."""
    clients = store.list()
    assert [c["client_id"] for c in clients] == ["cli-admin", "cli-001"]


def test_list_never_exposes_secret_hash(store, registered_client, admin_client):
    """Le hash du secret n'apparaît jamais dans list()."""
    for client in store.list():
        assert "secret_hash" not in client


def test_list_counts_are_consistent(store, registered_client, admin_client):
    """count / count_active / count_revoked sont cohérents avec list()."""
    assert store.count() == 2
    assert store.count_active() == 2
    assert store.count_revoked() == 0


# ============================================================================
# Révocation
# ============================================================================


def test_revoke_sets_revoked_flag_with_reason(store, registered_client):
    """revoke positionne revoked=True, revoked_at (ISO) et revoked_reason."""
    result = store.revoke(client_id="cli-001", reason="compromised_token")

    assert result["revoked"] is True
    assert result["revoked_reason"] == "compromised_token"
    assert result["revoked_at"] is not None
    assert "T" in result["revoked_at"]  # horodatage ISO 8601


def test_revoke_is_visible_in_list(store, registered_client):
    """Après révocation, list() reflète le statut et le motif."""
    store.revoke(client_id="cli-001", reason="abuse")
    clients = store.list()
    assert clients[0]["client_id"] == "cli-001"
    assert clients[0]["revoked"] is True
    assert clients[0]["revoked_reason"] == "abuse"


def test_revoke_updates_counts(store, registered_client):
    """count_active diminue et count_revoked augmente après révocation."""
    assert store.count_active() == 1
    assert store.count_revoked() == 0

    store.revoke(client_id="cli-001", reason="test")

    assert store.count_active() == 0
    assert store.count_revoked() == 1
    assert store.count() == 1  # total inchangé


def test_revoke_twice_raises(store, registered_client):
    """Révoquer un client déjà révoqué → MCPClientRevokedError."""
    store.revoke(client_id="cli-001", reason="first")
    with pytest.raises(MCPClientRevokedError, match="déjà révoqué"):
        store.revoke(client_id="cli-001", reason="second")


def test_revoke_unknown_client_raises(store):
    """Révoquer un client inexistant → MCPClientNotFoundError."""
    with pytest.raises(MCPClientNotFoundError):
        store.revoke(client_id="ghost", reason="pourquoi pas")


def test_revoke_with_blank_reason_raises(store, registered_client):
    """Un motif de révocation vide / blanc est rejeté (ValueError)."""
    with pytest.raises(ValueError, match="motif"):
        store.revoke(client_id="cli-001", reason="   ")


def test_revoke_invalidates_authentication(store, registered_client):
    """Après révocation, authenticate() échoue même avec le bon secret."""
    assert store.authenticate("cli-001", "secret-secret-1234") is True

    store.revoke(client_id="cli-001", reason="leaked")

    assert store.authenticate("cli-001", "secret-secret-1234") is False


# ============================================================================
# Métriques
# ============================================================================


def test_metrics_are_zero_for_fresh_client(store, registered_client):
    """Un client fraîchement inscrit a toutes ses métriques à zéro."""
    m = store.metrics(client_id="cli-001")
    assert m["client_id"] == "cli-001"
    assert m["call_count"] == 0
    assert m["error_count"] == 0
    assert m["error_rate"] == 0.0
    assert m["scope_usage"] == {}
    assert m["revoked"] is False


def test_metrics_compute_error_rate(store, registered_client):
    """error_rate = error_count / call_count, arrondi à 4 décimales."""
    store.record_call("cli-001", "tool_a", success=True)
    store.record_call("cli-001", "tool_b", success=False)
    store.record_call("cli-001", "tool_a", success=False)

    m = store.metrics(client_id="cli-001")
    assert m["call_count"] == 3
    assert m["error_count"] == 2
    assert m["error_rate"] == pytest.approx(2 / 3, abs=0.001)


def test_metrics_aggregate_scope_usage_by_tool(store, registered_client):
    """scope_usage compte les appels par nom de tool."""
    store.record_call("cli-001", "tool_x", success=True)
    store.record_call("cli-001", "tool_x", success=True)
    store.record_call("cli-001", "tool_y", success=False)

    m = store.metrics(client_id="cli-001")
    assert m["scope_usage"] == {"tool_x": 2, "tool_y": 1}


def test_metrics_still_readable_after_revocation(store, registered_client):
    """metrics() reste lisible pour un client révoqué (audit), avec statut."""
    store.record_call("cli-001", "tool_a", success=True)
    store.revoke(client_id="cli-001", reason="old")

    m = store.metrics(client_id="cli-001")
    assert m["call_count"] == 1
    assert m["revoked"] is True
    assert m["revoked_reason"] == "old"


def test_metrics_unknown_client_raises(store):
    """metrics() sur un client inexistant → MCPClientNotFoundError."""
    with pytest.raises(MCPClientNotFoundError):
        store.metrics(client_id="ghost")


# ============================================================================
# record_call
# ============================================================================


def test_record_call_increments_counters(store, registered_client):
    """record_call incrémente call_count et scope_usage[tool]."""
    store.record_call("cli-001", "search", success=True)

    m = store.metrics(client_id="cli-001")
    assert m["call_count"] == 1
    assert m["scope_usage"] == {"search": 1}


def test_record_call_failure_increments_error_count(store, registered_client):
    """record_call(success=False) incrémente error_count → error_rate=1.0."""
    store.record_call("cli-001", "tool", success=False)

    m = store.metrics(client_id="cli-001")
    assert m["error_count"] == 1
    assert m["error_rate"] == 1.0


def test_record_call_on_revoked_client_raises(store, registered_client):
    """record_call sur un client révoqué → MCPClientRevokedError."""
    store.revoke(client_id="cli-001", reason="banned")
    with pytest.raises(MCPClientRevokedError):
        store.record_call("cli-001", "tool", success=True)


def test_record_call_on_unknown_client_raises(store):
    """record_call sur un client inexistant → MCPClientNotFoundError."""
    with pytest.raises(MCPClientNotFoundError):
        store.record_call("ghost", "tool", success=True)


# ============================================================================
# Authentification
# ============================================================================


def test_authenticate_accepts_valid_credentials(store, registered_client):
    """authenticate() renvoie True pour le couple (client_id, secret) valide."""
    assert store.authenticate("cli-001", "secret-secret-1234") is True


def test_authenticate_rejects_wrong_secret(store, registered_client):
    """authenticate() renvoie False pour un secret erroné."""
    assert store.authenticate("cli-001", "wrong-secret") is False


def test_authenticate_rejects_unknown_client(store):
    """authenticate() renvoie False pour un client inexistant."""
    assert store.authenticate("ghost", "any-secret") is False


def test_authenticate_is_fail_closed_after_revocation(store, registered_client):
    """Un client révoqué n'est plus authentifiable (fail-closed)."""
    store.revoke(client_id="cli-001", reason="compromised")
    assert store.authenticate("cli-001", "secret-secret-1234") is False


# ============================================================================
# Scope d'un client (get_scope / get_by_client_id)
# ============================================================================


def test_get_scope_returns_registered_scope(store, registered_client):
    """get_scope() restitue le MCPSecurityScope persisté."""
    scope = store.get_scope(client_id="cli-001")
    assert isinstance(scope, MCPSecurityScope)
    assert scope.client_id == "cli-001"
    assert scope.role == "read_only"
    assert scope.tenant_id == "default"
    assert scope.is_active is True


def test_get_scope_rejects_unknown_client(store):
    """get_scope() sur un client inexistant → MCPClientNotFoundError."""
    with pytest.raises(MCPClientNotFoundError):
        store.get_scope(client_id="ghost")


def test_get_scope_rejects_revoked_client(store, registered_client):
    """get_scope() sur un client révoqué → MCPClientRevokedError."""
    store.revoke(client_id="cli-001", reason="revoked")
    with pytest.raises(MCPClientRevokedError):
        store.get_scope(client_id="cli-001")


def test_get_by_client_id_returns_public_dict(store, registered_client):
    """get_by_client_id() renvoie le dict public (sans secret_hash)."""
    result = store.get_by_client_id("cli-001")
    assert result is not None
    assert result["client_id"] == "cli-001"
    assert "secret_hash" not in result


def test_get_by_client_id_returns_none_for_unknown(store):
    """get_by_client_id() renvoie None pour un client inexistant."""
    assert store.get_by_client_id("ghost") is None


# ============================================================================
# Intégrité multi-clients & valeurs par défaut
# ============================================================================


def test_metrics_are_isolated_between_clients(store):
    """Les métriques de deux clients ne se mélangent jamais."""
    store.register("A", "sa", _make_scope("A"))
    store.register("B", "sb", _make_scope("B"))

    store.record_call("A", "tool_a", success=True)
    store.record_call("B", "tool_b", success=False)

    ma = store.metrics("A")
    mb = store.metrics("B")
    assert ma["call_count"] == 1
    assert ma["error_count"] == 0
    assert ma["scope_usage"] == {"tool_a": 1}
    assert mb["call_count"] == 1
    assert mb["error_count"] == 1
    assert mb["scope_usage"] == {"tool_b": 1}


def test_list_preserves_reverse_creation_order(store):
    """list() trie par created_at décroissant (plus récent d'abord)."""
    for i in range(5):
        store.register(f"order-{i}", f"s-{i}", _make_scope(f"order-{i}"))

    ids = [c["client_id"] for c in store.list()]
    assert ids == ["order-4", "order-3", "order-2", "order-1", "order-0"]


def test_default_scope_values_survive_round_trip(store):
    """Les defaults du scope (60 rpm, quota 5, read_only...) sont persistés."""
    result = store.register("defaults", "s", _make_scope("defaults"))

    assert result["tenant_id"] == "default"
    assert result["role"] == "read_only"
    assert result["visible_tools"] == []
    assert result["visible_resources"] == []
    assert result["visible_prompts"] == []
    assert result["sampling_enabled"] is False
    assert result["rate_limit_per_minute"] == 60
    assert result["destructive_quota"] == 5
    assert result["revoked"] is False
    assert result["revoked_at"] is None
    assert result["revoked_reason"] == ""


def test_all_scope_roles_round_trip(store):
    """Les 4 rôles (read_only→admin) sont acceptés et restitués tels quels."""
    for role in ("read_only", "contributor", "operator", "admin"):
        result = store.register(f"role-{role}", f"s-{role}", _make_scope(f"role-{role}", role=role))
        assert result["role"] == role


def test_scope_datetime_fields_are_iso_serializable(store, registered_client):
    """revoked_at en base est un ISO 8601 ; le scope désérialisé un datetime."""
    store.revoke(client_id="cli-001", reason="iso-check")
    result = store.get_by_client_id("cli-001")
    assert isinstance(result["revoked_at"], str)
    assert "T" in result["revoked_at"]

    # Le scope brut (modèle Pydantic) n'a pas de revoked_at positionné :
    # c'est la ligne SQL qui porte la révocation, le scope reste actif
    # tant que get_scope() n'est pas consulté (qui refuse les révoqués).
    scope = _make_scope("cli-001").revoke("reason", at=datetime(2026, 1, 1, tzinfo=None))
    assert scope.revoked_at == datetime(2026, 1, 1)




