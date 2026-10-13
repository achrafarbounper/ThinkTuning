# project/tests/test_mcp_tenant_isolation.py
"""Tests d'isolation multi-tenant MCP (MCP 2.3.0 — SCRUM-161).

Critères d'acceptation de la tâche :

    1. **Isolation inter-tenant** — un run estampillé par le tenant A est
       illisible/inannulable/inrepreneurable depuis le tenant B (l'erreur est
       IDENTIQUE à un run inconnu — aucun oracle) ; ``list_runs`` ne montre que
       le périmètre de l'appelant ; les runs legacy (sans estampille) ne sont
       accessibles que depuis le tenant par défaut ;
    2. **Vérification du scope** — client inconnu/révoqué, incohérence de
       tenant (en-tête ≠ scope) → refus fail-closed ; ``scope_for`` sert au
       filtrage ``resources/read`` ;
    3. **Vérification de l'alias** — ``stop_training`` → ``cancel_training`` :
       scope et quotas s'apprécient sur le nom CANONIQUE (un alias ne contourne
       jamais une vérification, ne double-compte pas un quota) ;
    4. **Limites JSON** — taille sérialisée + profondeur des arguments
       (parcours itératif : jamais de ``RecursionError``) ;
    5. **Rate limit & quotas** — débit per-client isolé, quota de coût par
       ``tenant:client`` (fenêtre glissante), quota de DURÉE des runs
       (``expired`` + raison dédiée).

Aucun appel réseau ni dépendance lourde : store SQLite temporaire, horloge
injectée, résolveur de scope en mémoire.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.application.mcp_orchestration import DURATION_QUOTA_EXCEEDED, MultiAgentMCPAdapter
from app.domain.entities.mcp import MCPResource, MCPScopeRole, MCPTool
from app.domain.ports.mcp_ports import MCPIdentity, MCPOrchestrationRequest, MCPSecurityScope
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.security.scope_enforcer import (
    MCPAccessDeniedError,
    MCPQuotaExceededError,
    MCPRateLimitExceededError,
    MCPScopeEnforcer,
)
from app.infrastructure.mcp.tenant_isolation import (
    DEFAULT_TENANT_ID,
    MCPArgumentLimitError,
    assert_run_owner,
    canonical_tool_name,
    check_json_arguments,
    max_json_bytes,
    max_json_depth,
    owner_matches,
)
from app.infrastructure.persistence.mcp_run_store import MCPDurableRunStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _NoopOrchestrator:
    """Orchestrateur minimal (jamais appelé par ces tests)."""

    def run(self, prompt, **kwargs):  # noqa: ANN001, ANN003
        return {"answer": "ok", "lead": {"status": "completed"}}

    def run_streaming(self, prompt, **kwargs):  # noqa: ANN001, ANN003
        kwargs["on_event"]("orchestrate.start", {"phase": "lead"})
        return {"answer": "ok", "lead": {"status": "completed"}}


def _store(tmp_path) -> MCPDurableRunStore:  # noqa: ANN001
    return MCPDurableRunStore(tmp_path / "mcp-runs.db")


def _identity(tenant: str = "t-a", client: str = "client-a", subject: str = "user-a"):
    return MCPIdentity(tenant_id=tenant, client_id=client, subject_id=subject)


def _request(**overrides: Any) -> MCPOrchestrationRequest:
    values: dict[str, Any] = {
        "prompt": "hello",
        "tenant_id": "t-a",
        "client_id": "client-a",
        "subject_id": "user-a",
    }
    values.update(overrides)
    return MCPOrchestrationRequest.from_values(**values)


def _scope(
    client_id: str,
    *,
    tenant_id: str = "t-a",
    role: str = "read_only",
    visible_tools: list[str] | None = None,
    visible_resources: list[str] | None = None,
    rate_limit_per_minute: int = 60,
    destructive_quota: int = 5,
    cost_quota_per_hour: int = 60,
    revoked: bool = False,
) -> MCPSecurityScope:
    return MCPSecurityScope(
        client_id=client_id,
        tenant_id=tenant_id,
        role=role,
        visible_tools=visible_tools or [],
        visible_resources=visible_resources or [],
        visible_prompts=[],
        sampling_enabled=False,
        rate_limit_per_minute=rate_limit_per_minute,
        destructive_quota=destructive_quota,
        cost_quota_per_hour=cost_quota_per_hour,
        revoked=revoked,
        revoked_at=datetime.now(UTC) if revoked else None,
        revoked_reason="test" if revoked else "",
    )


class _FakeClock:
    """Horloge monotone injectable (fenêtres glissantes des quotas)."""

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _rpc(server, request_id: int, method: str, params: dict | None = None) -> dict:
    return json.loads(
        server.handle_text(
            json.dumps(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
            )
        )
    )


# ===========================================================================
# 1. Isolation inter-tenant (runs durables)
# ===========================================================================


def test_run_is_stamped_with_caller_identity(tmp_path) -> None:
    """Tout run créé porte l'estampille (tenant / client / sujet) du créateur."""
    store = _store(tmp_path)
    adapter = MultiAgentMCPAdapter(_NoopOrchestrator(), store)
    result = adapter.run(_request(), on_event=lambda *_: None)
    state = store.get(result.run_id)
    assert state is not None
    assert (state.tenant_id, state.client_id, state.subject_id) == ("t-a", "client-a", "user-a")


def test_cross_tenant_read_is_unknown_run(tmp_path) -> None:
    """Le tenant B ne peut NI lire le run NI ses événements du tenant A."""
    store = _store(tmp_path)
    adapter = MultiAgentMCPAdapter(_NoopOrchestrator(), store)
    result = adapter.run(_request(), on_event=lambda *_: None)
    run_id = result.run_id
    outsider = _identity("t-b", "client-b")

    with pytest.raises(KeyError, match="unknown MCP run"):
        adapter.get_run(run_id, identity=outsider)
    with pytest.raises(KeyError, match="unknown MCP run"):
        adapter.get_events(run_id, identity=outsider)

    # Le propriétaire, lui, lit normalement.
    assert adapter.get_run(run_id, identity=_identity()) is not None


def test_cross_tenant_cancel_and_retry_are_unknown_run(tmp_path) -> None:
    """Cancel/retry cross-tenant → même erreur qu'un run inexistant, zéro mutation."""
    store = _store(tmp_path)
    adapter = MultiAgentMCPAdapter(_NoopOrchestrator(), store)
    store.create("run-x", owner=_identity())
    outsider = _identity("t-b", "client-b")

    with pytest.raises(KeyError, match="unknown MCP run"):
        adapter.cancel("run-x", reason="sabotage", identity=outsider)
    assert store.get("run-x").state == "pending"  # AUCUNE mutation subie

    store.transition("run-x", "running")
    store.transition("run-x", "failed")
    with pytest.raises(KeyError, match="unknown MCP run"):
        adapter.retry("run-x", identity=outsider)


def test_same_tenant_other_client_is_denied(tmp_path) -> None:
    """La reprise appartient au CRÉATEUR : un autre client du même tenant est refusé."""
    store = _store(tmp_path)
    adapter = MultiAgentMCPAdapter(_NoopOrchestrator(), store)
    store.create("run-y", owner=_identity("t-a", "client-a"))
    with pytest.raises(KeyError, match="unknown MCP run"):
        adapter.get_run("run-y", identity=_identity("t-a", "client-b"))


def test_list_runs_is_scoped_to_caller_scope(tmp_path) -> None:
    """``list_runs`` ne contient QUE les runs du périmètre de l'appelant."""
    store = _store(tmp_path)
    adapter = MultiAgentMCPAdapter(_NoopOrchestrator(), store)
    store.create("run-a", owner=_identity("t-a", "client-a"))
    store.create("run-b", owner=_identity("t-b", "client-b"))
    store.create("run-legacy")  # sans estampille

    mine = {run["run_id"] for run in adapter.list_runs(identity=_identity())}
    assert mine == {"run-a"}

    other = {run["run_id"] for run in adapter.list_runs(identity=_identity("t-b", "client-b"))}
    assert other == {"run-b"}


def test_legacy_run_readable_only_from_default_tenant(tmp_path) -> None:
    """Un run SANS estampille (antérieur à 2.3.0) est cloisonné au tenant défaut."""
    store = _store(tmp_path)
    adapter = MultiAgentMCPAdapter(_NoopOrchestrator(), store)
    store.create("legacy-run")

    # Tenant explicite ≠ défaut → refus (indiscernable d'un run inconnu).
    with pytest.raises(KeyError, match="unknown MCP run"):
        adapter.get_run("legacy-run", identity=_identity("t-a", "client-a"))
    # Tenant défaut → lisible (continuité opérationnelle des runs existants).
    assert adapter.get_run("legacy-run", identity=_identity(DEFAULT_TENANT_ID, "ops")) is not None
    # Appelant NON déclaré (historique) → comportement 2.2.x inchangé.
    assert adapter.get_run("legacy-run", identity=None) is not None


def test_prepare_run_stamps_owner_and_guards_resume(tmp_path) -> None:
    """La PRÉPARATION SSE estampille le run et garde la reprise cross-tenant."""
    store = _store(tmp_path)
    adapter = MultiAgentMCPAdapter(_NoopOrchestrator(), store)

    snapshot = adapter.prepare_run(_request(tenant_id="t-c", client_id="client-c"))
    state = store.get(snapshot["run_id"])
    assert (state.tenant_id, state.client_id) == ("t-c", "client-c")

    # Reprise préparée depuis un autre tenant → refus AVANT le premier octet.
    intruder = _request(run_id=snapshot["run_id"], tenant_id="t-b", client_id="client-b")
    with pytest.raises(KeyError, match="unknown MCP run"):
        adapter.prepare_run(intruder)


def test_owner_guards_are_fail_closed() -> None:
    """``owner_matches`` : garde pure — sujet différent, cross-tenant, etc."""
    owner = _identity("t-a", "client-a", "user-a")
    assert owner_matches(_identity("t-a", "client-a", "user-a"), owner)
    assert not owner_matches(_identity("t-a", "client-a", "user-b"), owner)
    assert not owner_matches(_identity("t-b", "client-a", "user-a"), owner)
    # Un client sans sujet n'est PAS bloqué par l'estampille subject du run.
    assert owner_matches(_identity("t-a", "client-a"), owner)
    with pytest.raises(PermissionError, match="outside the caller"):
        assert_run_owner(_identity("t-b", "client-b"), owner, run_id="r1")


# ===========================================================================
# 2. Vérification du scope (enforceur)
# ===========================================================================


def test_scope_unknown_client_is_denied() -> None:
    """Fail-closed : un client absent du store est refusé, même en lecture."""
    enforcer = MCPScopeEnforcer(scope_resolver=lambda _cid: None)
    with pytest.raises(MCPAccessDeniedError):
        enforcer.check_scope("ghost", "list_dir", tenant_id="t-a")


def test_scope_tenant_mismatch_is_denied() -> None:
    """Le tenant DÉCLARÉ (en-tête) doit correspondre au tenant du scope."""
    scopes = {"client-a": _scope("client-a", tenant_id="t-a", visible_tools=["list_dir"])}
    enforcer = MCPScopeEnforcer(scope_resolver=scopes.get)
    enforcer.check_scope("client-a", "list_dir", tenant_id="t-a")  # cohérent → OK
    with pytest.raises(MCPAccessDeniedError):
        enforcer.check_scope("client-a", "list_dir", tenant_id="t-evil")


def test_scope_for_returns_scope_when_tenant_coherent() -> None:
    """``scope_for`` (filtrage resources/read) : cohérent → scope ; sinon refus."""
    scopes = {
        "client-a": _scope(
            "client-a", tenant_id="t-a", visible_resources=["thinktuning://jobs"]
        )
    }
    enforcer = MCPScopeEnforcer(scope_resolver=scopes.get)
    assert enforcer.scope_for("client-a", tenant_id="t-a").client_id == "client-a"
    with pytest.raises(MCPAccessDeniedError):
        enforcer.scope_for("client-a", tenant_id="other")


def test_scope_revoked_client_is_denied() -> None:
    scopes = {"client-r": _scope("client-r", revoked=True)}
    enforcer = MCPScopeEnforcer(scope_resolver=scopes.get)
    with pytest.raises(MCPAccessDeniedError):
        enforcer.check_scope("client-r", "list_dir")


# ===========================================================================
# 3. Vérification de l'alias (stop_training → cancel_training)
# ===========================================================================


def test_alias_resolves_to_canonical_name() -> None:
    assert canonical_tool_name("stop_training") == "cancel_training"
    assert canonical_tool_name("cancel_training") == "cancel_training"
    # Un nom inconnu passe tel quel (la visibilité est jugée ailleurs).
    assert canonical_tool_name("list_dir") == "list_dir"


def test_alias_does_not_bypass_scope() -> None:
    """Un alias dont le CANONIQUE est hors whitelist ne contourne pas le refus."""
    # Whitelist ne contenant QUE l'alias (pas le canonique) : le check porte
    # sur ``cancel_training`` → refus.
    scopes = {"client-a": _scope("client-a", visible_tools=["stop_training"])}
    enforcer = MCPScopeEnforcer(scope_resolver=scopes.get)
    with pytest.raises(MCPAccessDeniedError):
        enforcer.check_scope("client-a", "stop_training")

    # Whitelist contenant le canonique : l'alias est jugé comme lui → OK.
    scopes_ok = {"client-a": _scope("client-a", visible_tools=["cancel_training"])}
    enforcer_ok = MCPScopeEnforcer(scope_resolver=scopes_ok.get)
    enforcer_ok.check_scope("client-a", "stop_training")  # ne lève pas


def test_alias_quota_is_counted_on_canonical_name() -> None:
    """Le quota destructif est consommé par le nom CANONIQUE (pas de double comptage).

    ``stop_training`` puis ``cancel_training`` touchent la MÊME enveloppe :
    un quota de 1 → le deuxième appel (par l'alias ou le canonique) est refusé.
    """
    clock = _FakeClock()
    scopes = {
        "client-a": _scope(
            "client-a", visible_tools=["cancel_training"], destructive_quota=1
        )
    }
    enforcer = MCPScopeEnforcer(scope_resolver=scopes.get, clock=clock)
    enforcer.check_quota("client-a", "stop_training", tenant_id="t-a")
    with pytest.raises(MCPQuotaExceededError):
        enforcer.check_quota("client-a", "cancel_training", tenant_id="t-a")
    # La fenêtre glissante rend le slot à nouveau disponible.
    clock.advance(3600)
    enforcer.check_quota("client-a", "stop_training", tenant_id="t-a")


# ===========================================================================
# 4. Limites de taille et de profondeur du JSON (orchestrate)
# ===========================================================================


def test_json_size_limit_is_enforced() -> None:
    payload = {"prompt": "x" * 1000}
    check_json_arguments(payload, max_bytes=10_000)  # dans les limites → OK
    with pytest.raises(MCPArgumentLimitError, match="size limit"):
        check_json_arguments(payload, max_bytes=100)


def test_json_depth_limit_is_enforced_without_recursion_error() -> None:
    """Une structure profonde est rejetée NETTEMENT (jamais de ``RecursionError``)."""
    deep: dict[str, Any] = {"value": 1}
    for _ in range(5000):
        deep = {"child": deep}
    with pytest.raises(MCPArgumentLimitError, match="depth limit"):
        check_json_arguments(deep, max_depth=8)


def test_json_limits_configurable_via_env(monkeypatch) -> None:
    monkeypatch.setenv("MCP_MAX_JSON_BYTES", "2000")
    monkeypatch.setenv("MCP_MAX_JSON_DEPTH", "6")
    assert max_json_bytes() == 2000
    assert max_json_depth() == 6
    with pytest.raises(MCPArgumentLimitError, match="depth limit"):
        check_json_arguments({"a": {"b": {"c": {"d": {"e": {"f": {"g": 1}}}}}}})  # 7 niveaux
    check_json_arguments({"a": {"b": {"c": {"d": {"e": 1}}}}})  # 5 niveaux → OK
    # Bornes minimales : une config absurde est remontée au plancher sûr.
    monkeypatch.setenv("MCP_MAX_JSON_BYTES", "1")
    monkeypatch.setenv("MCP_MAX_JSON_DEPTH", "0")
    assert max_json_bytes() == 1024
    assert max_json_depth() == 4


# ===========================================================================
# 5. Rate limit, quota de coût et quota de durée
# ===========================================================================


def test_rate_limit_burst_is_rejected(monkeypatch) -> None:
    """Le débit est borné par ``rate_limit_per_minute`` ; refill après délai."""
    import time as time_module

    now = [1000.0]
    monkeypatch.setattr(time_module, "monotonic", lambda: now[0])
    scopes = {"client-a": _scope("client-a", rate_limit_per_minute=2)}
    enforcer = MCPScopeEnforcer(scope_resolver=scopes.get)
    enforcer.check_rate_limit("client-a")
    enforcer.check_rate_limit("client-a")
    with pytest.raises(MCPRateLimitExceededError, match="Rate limit") as excinfo:
        enforcer.check_rate_limit("client-a")
    assert excinfo.value.retry_after >= 1
    # Refill après délai (jetons rendus par le seau à jetons).
    now[0] += 31
    enforcer.check_rate_limit("client-a")


def test_rate_limit_buckets_are_isolated_per_client() -> None:
    scopes = {
        "client-a": _scope("client-a", rate_limit_per_minute=1),
        "client-b": _scope("client-b", rate_limit_per_minute=1),
    }
    enforcer = MCPScopeEnforcer(scope_resolver=scopes.get)
    enforcer.check_rate_limit("client-a")
    # L'épuisement du client A ne pénalise PAS le client B (isolation).
    enforcer.check_rate_limit("client-b")
    with pytest.raises(MCPRateLimitExceededError):
        enforcer.check_rate_limit("client-a")


def test_cost_quota_is_scoped_per_tenant_and_client() -> None:
    """Le quota de coût (orchestrate) est fenêtré par ``tenant:client``."""
    clock = _FakeClock()
    scopes = {
        "client-a": _scope("client-a", tenant_id="t-a", cost_quota_per_hour=2),
        "client-b": _scope("client-b", tenant_id="t-b", cost_quota_per_hour=2),
    }
    enforcer = MCPScopeEnforcer(scope_resolver=scopes.get, clock=clock)
    enforcer.check_cost_quota("client-a", "orchestrate", tenant_id="t-a")
    enforcer.check_cost_quota("client-a", "orchestrate", tenant_id="t-a")
    with pytest.raises(MCPQuotaExceededError, match="co[uû]t"):
        enforcer.check_cost_quota("client-a", "orchestrate", tenant_id="t-a")
    # Le client B (autre tenant) dispose de sa PROPRE enveloppe.
    enforcer.check_cost_quota("client-b", "orchestrate", tenant_id="t-b")
    # Les tools NON agentiques ne consomment jamais de coût.
    enforcer.check_cost_quota("client-a", "list_dir", tenant_id="t-a")


def test_cost_quota_window_is_sliding() -> None:
    clock = _FakeClock()
    scopes = {"client-a": _scope("client-a", cost_quota_per_hour=1)}
    enforcer = MCPScopeEnforcer(scope_resolver=scopes.get, clock=clock)
    enforcer.check_cost_quota("client-a", "orchestrate")
    with pytest.raises(MCPQuotaExceededError):
        enforcer.check_cost_quota("client-a", "orchestrate")
    clock.advance(3600)  # l'appel le plus ancien sort de la fenêtre
    enforcer.check_cost_quota("client-a", "orchestrate")


def test_duration_quota_expires_stale_run(tmp_path, monkeypatch) -> None:
    """Un run non terminal au-delà de ``max_run_seconds`` est EXPIRÉ (raison dédiée)."""
    store = _store(tmp_path)
    adapter = MultiAgentMCPAdapter(_NoopOrchestrator(), store, max_run_seconds=10)
    store.create("stale-run", request_fingerprint="fp", owner=_identity())

    class _FutureDatetime(datetime):
        """Horloge décalée : ``datetime.now`` voit le run âgé d'une heure."""

        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            return datetime.now(tz or UTC) + timedelta(hours=1)

    monkeypatch.setattr("app.application.mcp_orchestration.datetime", _FutureDatetime)

    with pytest.raises(ValueError, match="duration quota"):
        adapter.run(_request(run_id="stale-run"), on_event=lambda *_: None)

    expired = store.get("stale-run")
    assert expired.state == "expired"
    assert expired.last_error == DURATION_QUOTA_EXCEEDED
    assert expired.is_retryable  # réessayable via ``runs/retry``


# ===========================================================================
# 6. Intégration serveur (``handle_text`` avec identité déclarée)
# ===========================================================================


class _SingleToolProvider:
    """Registre minimal : un tool de lecture, handler vérifiable."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_tools(self) -> list[MCPTool]:
        return [
            MCPTool(
                name="list_dir",
                description="test tool",
                input_schema={"type": "object", "properties": {}},
                annotations={
                    "readOnlyHint": True,
                    "destructiveHint": False,
                    "idempotentHint": True,
                },
                required_scope=MCPScopeRole.READ_ONLY,
                handler=self._call,
            )
        ]

    def _call(self, arguments: dict[str, Any]) -> dict:
        self.calls.append(str(arguments))
        return {"ok": True}


class _FakeResourceRegistry:
    """Registre de resources minimal (une URI statique)."""

    def list_resources(self) -> list[MCPResource]:
        return [
            MCPResource(
                uri="thinktuning://jobs",
                name="jobs",
                description="test",
                mime_type="application/json",
            )
        ]

    def read_resource(self, uri: str) -> str:
        if uri == "thinktuning://jobs":
            return "[]"
        raise KeyError(uri)


class _EmptyPrompts:
    def list_prompts(self):
        return []

    def get_prompt(self, name, arguments=None):
        raise KeyError(name)


def _server() -> Any:
    return build_mcp_server(
        tool_provider=_SingleToolProvider(),
        resource_provider=_FakeResourceRegistry(),
        prompt_provider=_EmptyPrompts(),
    )


@pytest.fixture()
def _isolated_enforcer(monkeypatch):
    """Enforceur partagé remplacé par une instance de test (isolation totale)."""
    scopes = {
        "client-a": _scope("client-a", tenant_id="t-a", visible_tools=["list_dir"]),
        "client-b": _scope(
            "client-b",
            tenant_id="t-b",
            visible_tools=["list_dir"],
            visible_resources=["thinktuning://jobs"],
        ),
    }
    from app.infrastructure.mcp.security import scope_enforcer as se

    enforcer = MCPScopeEnforcer(scope_resolver=scopes.get)
    monkeypatch.setattr(se, "_default_enforcer", enforcer)
    return enforcer


def test_server_tools_call_enforces_security_gate(_isolated_enforcer) -> None:
    """``tools/call`` avec identité déclarée : client inconnu → refus, connu → OK."""
    server = _server()
    # Appel SANS identité → comportement 2.2.x (aucune garde) : témoin.
    witness = _rpc(server, 1, "tools/call", {"name": "list_dir", "arguments": {}})
    assert "result" in witness

    raw = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_dir", "arguments": {}},
        }
    )
    # Client inconnu du store, identité déclarée → refus fail-closed.
    denied = json.loads(
        server.handle_text(
            raw,
            client_id="unknown-client",
            identity=MCPIdentity(tenant_id="t-a", client_id="unknown-client", subject_id="u"),
        )
    )
    assert "error" in denied

    # Client déclaré et cohérent → l'appel proceed normalement.
    allowed = json.loads(
        server.handle_text(
            raw,
            client_id="client-a",
            identity=MCPIdentity(tenant_id="t-a", client_id="client-a"),
        )
    )
    assert "result" in allowed


def test_server_tools_call_gate_respects_tenant_mismatch(_isolated_enforcer) -> None:
    """En-tête tenant ≠ tenant du scope → refus (aucune exception à la règle)."""
    server = _server()
    raw = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "list_dir", "arguments": {}},
        }
    )
    denied = json.loads(
        server.handle_text(
            raw,
            client_id="client-a",
            identity=MCPIdentity(tenant_id="t-evil", client_id="client-a"),
        )
    )
    assert "error" in denied


def test_server_resources_read_filters_by_whitelist(_isolated_enforcer, monkeypatch) -> None:
    """``resources/read`` : hors whitelist → indiscernable d'une URI inconnue."""
    server = _server()
    raw = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "resources/read",
            "params": {"uri": "thinktuning://jobs"},
        }
    )
    # Client B : whitelist contenant l'URI → lecture autorisée.
    allowed = json.loads(
        server.handle_text(
            raw,
            client_id="client-b",
            identity=MCPIdentity(tenant_id="t-b", client_id="client-b"),
        )
    )
    assert "result" in allowed

    # Client A avec une whitelist EXCLURANTE → refus « Unknown resource ».
    from app.infrastructure.mcp.security import scope_enforcer as se

    scopes = {
        "client-a": _scope(
            "client-a", tenant_id="t-a", visible_resources=["thinktuning://models"]
        )
    }
    monkeypatch.setattr(se, "_default_enforcer", MCPScopeEnforcer(scope_resolver=scopes.get))
    denied = json.loads(
        server.handle_text(
            raw,
            client_id="client-a",
            identity=MCPIdentity(tenant_id="t-a", client_id="client-a"),
        )
    )
    assert "error" in denied
    assert "Unknown resource" in denied["error"]["message"]

    # L'appelant NON déclaré conserve le comportement 2.2.x (aucun filtrage).
    monkeypatch.setattr(se, "_default_enforcer", _isolated_enforcer)
    legacy = json.loads(server.handle_text(raw, client_id="anonymous"))
    assert "result" in legacy


def test_server_rejects_oversized_arguments(_isolated_enforcer, monkeypatch) -> None:
    """``tools/call`` : arguments trop gros → ``validation_error`` réparable."""
    monkeypatch.setenv("MCP_MAX_JSON_BYTES", "200")
    server = _server()
    response = _rpc(
        server,
        3,
        "tools/call",
        {"name": "list_dir", "arguments": {"blob": "x" * 5000}},
    )
    assert "error" in response
    data = response["error"].get("data") or {}
    assert "arguments" in json.dumps(data)







