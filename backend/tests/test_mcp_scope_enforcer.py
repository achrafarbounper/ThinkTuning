# project/tests/test_mcp_scope_enforcer.py
"""Tests de l'enforceur MCP — scopes, quotas, rate limiting (S4, tâche 11).

Checklist (docs/mcp/IMPLEMENTATION_PLAN.md, tâche 11) :
    - 4 rôles : ``read_only`` (12 tools), ``contributor`` (25), ``operator``
      (35), ``admin`` (40) — tailles des catalogues + ordre de privilège ;
    - ``check_scope``   : chaque rôle (autorisé / refusé), whitelist
      ``visible_tools`` (vide → catalogue du rôle), client inconnu, client
      révoqué, rôle inconnu (tous fail-closed) ;
    - ``check_quota``   : ``destructive_quota`` par heure — dépassement, quota 0,
      isolation par client, fenêtre glissante (clock injectée), tools de
      lecture jamais comptés ;
    - ``check_rate_limit`` : ``rate_limit_per_minute`` — burst, isolation,
      refill après délai ;
    - intégration : la primitive ``TokenBucket`` est la MÊME que celle du
      middleware REST ``api/middlewares/rate_limit.py`` (zéro duplication).

Aucun appel réseau ni dépendance lourde dans cette suite.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.ports.mcp_ports import MCPSecurityScope
from app.infrastructure.mcp.security.rate_limit_bucket import TokenBucket
from app.infrastructure.mcp.security.scope_enforcer import (
    ADMIN_ROLE_TOOLS,
    CONTRIBUTOR_ROLE_TOOLS,
    MCPAccessDeniedError,
    MCPQuotaExceededError,
    MCPRateLimitExceededError,
    MCPScopeEnforcer,
    OPERATOR_ROLE_TOOLS,
    READ_ONLY_ROLE_TOOLS,
    ROLE_TOOLS,
    check_quota,
    check_rate_limit,
    check_scope,
    effective_tools,
    enforce,
    get_default_enforcer,
    is_destructive_tool,
    reset_default_enforcer,
    resolve_scope,
)


def _scope(
    client_id: str,
    *,
    role: str = "read_only",
    visible_tools: list[str] | None = None,
    rate_limit_per_minute: int = 60,
    destructive_quota: int = 5,
    revoked: bool = False,
) -> MCPSecurityScope:
    """Fabrique un ``MCPSecurityScope`` de test avec des valeurs par défaut sûres."""
    return MCPSecurityScope(
        client_id=client_id,
        tenant_id="default",
        role=role,
        visible_tools=visible_tools or [],
        visible_resources=[],
        visible_prompts=[],
        sampling_enabled=False,
        rate_limit_per_minute=rate_limit_per_minute,
        destructive_quota=destructive_quota,
        revoked=revoked,
        revoked_at=datetime(2026, 1, 1, tzinfo=timezone.utc) if revoked else None,
        revoked_reason="test-revocation" if revoked else "",
    )


def _enforcer(
    scopes: dict[str, MCPSecurityScope], **kwargs: object
) -> MCPScopeEnforcer:
    """Enforceur sur un résolveur dict en mémoire (isolation par test)."""
    return MCPScopeEnforcer(scope_resolver=lambda client_id: scopes.get(client_id), **kwargs)


class _FakeClock:
    """Horloge monotone injectable pour la fenêtre de quota."""

    def __init__(self) -> None:
        self._now = 0.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


# ============================================================================
# Rôles — catalogues roadmap (tailles 12 / 25 / 35 / 40)
# ============================================================================


def test_role_catalog_sizes_match_roadmap() -> None:
    """Les 4 rôles tiennent les tailles cibles de la roadmap (12/25/35/40)."""
    assert len(READ_ONLY_ROLE_TOOLS) == 12
    assert len(CONTRIBUTOR_ROLE_TOOLS) == 25
    assert len(OPERATOR_ROLE_TOOLS) == 35
    assert len(ADMIN_ROLE_TOOLS) == 40
    assert {
        "read_only": 12,
        "contributor": 25,
        "operator": 35,
        "admin": 40,
    } == {name: len(tools) for name, tools in ROLE_TOOLS.items()}


def test_role_catalogs_are_strictly_cumulative() -> None:
    """read_only ⊂ contributor ⊂ operator ⊂ admin (privilège croissant)."""
    assert READ_ONLY_ROLE_TOOLS <= CONTRIBUTOR_ROLE_TOOLS
    assert CONTRIBUTOR_ROLE_TOOLS <= OPERATOR_ROLE_TOOLS
    assert OPERATOR_ROLE_TOOLS <= ADMIN_ROLE_TOOLS
    assert READ_ONLY_ROLE_TOOLS < CONTRIBUTOR_ROLE_TOOLS
    assert CONTRIBUTOR_ROLE_TOOLS < OPERATOR_ROLE_TOOLS
    assert OPERATOR_ROLE_TOOLS < ADMIN_ROLE_TOOLS


def test_specific_role_boundary_tools() -> None:
    """Points de frontière : write/exec/training sont aux bons endroits du catalogue.

    Le catalogue suit la roadmap versionnée : operator = contributor + 10 tools
    (tâche 17, dont ``cancel_training``/``stop_training``) ; admin = operator + 5
    tools de gestion mutante (tâche 19 : move_path, remove_path, split_file,
    dedupe_lines, unzip_file).
    """
    assert "write_file" not in READ_ONLY_ROLE_TOOLS
    assert "write_file" not in CONTRIBUTOR_ROLE_TOOLS  # tâche 17 → operator
    assert "write_file" in OPERATOR_ROLE_TOOLS
    assert "run_command" in OPERATOR_ROLE_TOOLS
    assert "run_command" not in CONTRIBUTOR_ROLE_TOOLS
    assert "cancel_training" in OPERATOR_ROLE_TOOLS  # tâche 17 (pilotage training)
    assert "remove_path" in ADMIN_ROLE_TOOLS
    assert "remove_path" not in OPERATOR_ROLE_TOOLS
    assert "unzip_file" in ADMIN_ROLE_TOOLS
    assert "unzip_file" not in OPERATOR_ROLE_TOOLS


def test_read_only_tools_are_lecture_seule() -> None:
    """Les 12 tools read_only ne contiennent aucune mutation connue."""
    for name in READ_ONLY_ROLE_TOOLS:
        assert is_destructive_tool(name) is False, name


# ============================================================================
# check_scope — chaque rôle
# ============================================================================


def test_each_role_allows_its_full_catalog() -> None:
    """Chaque rôle VOIT TOUT son catalogue (check_scope sans exception)."""
    for role, catalog in ROLE_TOOLS.items():
        enforcer = _enforcer({f"cli-{role}": _scope(f"cli-{role}", role=role)})
        for tool in catalog:
            enforcer.check_scope(f"cli-{role}", tool)


def test_read_only_denies_write_tool() -> None:
    """Un client read_only ne peut pas appeler un tool d'écriture."""
    enforcer = _enforcer({"ro": _scope("ro", role="read_only")})
    with pytest.raises(MCPAccessDeniedError):
        enforcer.check_scope("ro", "write_file")


def test_contributor_denies_operator_and_admin_tools() -> None:
    """contributor refuse exec (operator) et les tools de gestion mutante (admin)."""
    enforcer = _enforcer({"co": _scope("co", role="contributor")})
    with pytest.raises(MCPAccessDeniedError):
        enforcer.check_scope("co", "run_command")  # exec → operator
    with pytest.raises(MCPAccessDeniedError):
        enforcer.check_scope("co", "remove_path")  # tâche 19 → admin


def test_operator_denies_admin_tools() -> None:
    """operator refuse les tools réservés admin (tâche 19)."""
    enforcer = _enforcer({"op": _scope("op", role="operator")})
    enforcer.check_scope("op", "write_file")  # write → operator ✓
    with pytest.raises(MCPAccessDeniedError):
        enforcer.check_scope("op", "remove_path")


def test_admin_allows_catalog_and_rejects_unknown() -> None:
    """admin voit tout le catalogue, mais un tool inexistant reste refusé."""
    enforcer = _enforcer({"ad": _scope("ad", role="admin")})
    enforcer.check_scope("ad", "unzip_file")
    with pytest.raises(MCPAccessDeniedError):
        enforcer.check_scope("ad", "totally_unknown_tool")


# ============================================================================
# check_scope — cas de dépassement / fail-closed
# ============================================================================


def test_unknown_client_fail_closed() -> None:
    """Un client inconnu est refusé partout (aucun oracle)."""
    enforcer = _enforcer({})
    with pytest.raises(MCPAccessDeniedError):
        enforcer.check_scope("ghost", "read_file")
    with pytest.raises(MCPAccessDeniedError):
        enforcer.check_rate_limit("ghost")
    with pytest.raises(MCPAccessDeniedError):
        resolve_scope("ghost", resolver=lambda _: None)


def test_revoked_client_fail_closed() -> None:
    """La révocation prime : même admin, aucun tool n'est visible."""
    enforcer = _enforcer({"bad": _scope("bad", role="admin", revoked=True)})
    with pytest.raises(MCPAccessDeniedError):
        enforcer.check_scope("bad", "read_file")


def test_unknown_role_fail_closed() -> None:
    """Un rôle hors catalogue est refusé (comparaison stricte)."""
    scope = _scope("weird", role="superuser")
    enforcer = _enforcer({"weird": scope})
    with pytest.raises(MCPAccessDeniedError):
        enforcer.check_scope("weird", "read_file")


def test_whitelist_overrides_role_catalog() -> None:
    """``visible_tools`` non vide = whitelist STRICTE (même admin)."""
    scope = _scope("w", role="contributor", visible_tools=["read_file", "write_file"])
    enforcer = _enforcer({"w": scope})
    enforcer.check_scope("w", "write_file")  # whitelist explicite
    with pytest.raises(MCPAccessDeniedError):
        enforcer.check_scope("w", "calc")  # hors whitelist malgré le rôle


def test_empty_whitelist_falls_back_to_role_catalog() -> None:
    """``visible_tools`` vide = catalogue du rôle (docs/mcp/MCP_SECURITY.md)."""
    enforcer = _enforcer({"e": _scope("e", role="read_only")})
    enforcer.check_scope("e", "read_file")
    with pytest.raises(MCPAccessDeniedError):
        enforcer.check_scope("e", "write_file")


def test_effective_tools_prefers_whitelist() -> None:
    """``effective_tools`` : whitelist si non vide, sinon catalogue du rôle."""
    scope = _scope("t", role="read_only", visible_tools=["read_file"])
    assert effective_tools(scope) == frozenset({"read_file"})
    assert effective_tools(_scope("t2", role="read_only")) == READ_ONLY_ROLE_TOOLS


def test_allowed_tools_client_accessor() -> None:
    """``allowed_tools(client_id)`` expose la portée effective du client."""
    enforcer = _enforcer({"r": _scope("r", role="read_only")})
    assert enforcer.allowed_tools("r") == READ_ONLY_ROLE_TOOLS


# ============================================================================
# check_quota — destructive_quota
# ============================================================================


def test_quota_blocks_after_destructive_limit() -> None:
    """Au-delà du quota « manual approval », l'appel destructif est refusé."""
    scope = _scope("q", role="operator", destructive_quota=2)
    enforcer = _enforcer({"q": scope})
    enforcer.check_quota("q", "write_file")  # slot 1/2
    enforcer.check_quota("q", "write_file")  # slot 2/2
    with pytest.raises(MCPQuotaExceededError):
        enforcer.check_quota("q", "write_file")  # → dépassement


def test_quota_ignores_read_only_tools() -> None:
    """Les tools de lecture pure ne consomment jamais de quota destructif."""
    scope = _scope("q2", role="operator", destructive_quota=1)
    enforcer = _enforcer({"q2": scope})
    for _ in range(5):
        enforcer.check_quota("q2", "read_file")
    enforcer.check_quota("q2", "write_file")  # 1er destructif → slot 1/1
    with pytest.raises(MCPQuotaExceededError):
        enforcer.check_quota("q2", "write_file")


def test_quota_window_expires_after_hour() -> None:
    """La fenêtre glissante d'une heure libère le quota (clock injectée)."""
    clock = _FakeClock()
    enforcer = _enforcer(
        {"w": _scope("w", role="operator", destructive_quota=1)}, clock=clock
    )
    enforcer.check_quota("w", "write_file")  # t=0 → slot 1/1
    with pytest.raises(MCPQuotaExceededError):
        enforcer.check_quota("w", "write_file")  # t=0 → épuisé
    clock.advance(3601)  # fenêtre 3600s dépassée
    enforcer.check_quota("w", "write_file")  # slot libéré


def test_quota_isolated_per_client() -> None:
    """Le quota d'un client n'affecte jamais celui d'un autre."""
    enforcer = _enforcer(
        {
            "a": _scope("a", role="operator", destructive_quota=1),
            "b": _scope("b", role="operator", destructive_quota=1),
        }
    )
    enforcer.check_quota("a", "write_file")
    enforcer.check_quota("b", "write_file")  # indépendant
    with pytest.raises(MCPQuotaExceededError):
        enforcer.check_quota("a", "write_file")


def test_quota_zero_always_denies_destructive() -> None:
    """``destructive_quota=0`` : aucun outil destructif n'est autorisé."""
    enforcer = _enforcer({"z": _scope("z", role="operator", destructive_quota=0)})
    with pytest.raises(MCPQuotaExceededError):
        enforcer.check_quota("z", "write_file")
    enforcer.check_quota("z", "read_file")  # lecture jamais bloquée par le quota


def test_unknown_tool_is_fail_closed_destructive() -> None:
    """Un tool inconnu est traité comme destructif (le doute n'est pas résolu)."""
    assert is_destructive_tool("no_such_tool") is True


# ============================================================================
# check_rate_limit — rate_limit_per_minute
# ============================================================================


def test_rate_limit_blocks_after_burst() -> None:
    """Le débit est borné par ``rate_limit_per_minute`` (burst initial)."""
    enforcer = _enforcer({"rl": _scope("rl", role="read_only", rate_limit_per_minute=3)})
    for _ in range(3):
        enforcer.check_rate_limit("rl")
    with pytest.raises(MCPRateLimitExceededError) as excinfo:
        enforcer.check_rate_limit("rl")
    assert excinfo.value.retry_after >= 1


def test_rate_limit_isolated_per_client() -> None:
    """Chaque client a son propre bucket (pas de collision entre clients)."""
    enforcer = _enforcer(
        {
            "a": _scope("a", role="read_only", rate_limit_per_minute=1),
            "b": _scope("b", role="read_only", rate_limit_per_minute=1),
        }
    )
    enforcer.check_rate_limit("a")
    enforcer.check_rate_limit("b")  # indépendant
    with pytest.raises(MCPRateLimitExceededError):
        enforcer.check_rate_limit("a")


def test_rate_limit_refills_after_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Après un délai, le seau se remplit et le client est de nouveau admis."""
    import time as time_module

    now = [1000.0]
    monkeypatch.setattr(time_module, "monotonic", lambda: now[0])
    try:
        enforcer = _enforcer(
            {"ref": _scope("ref", role="read_only", rate_limit_per_minute=2)}
        )
        enforcer.check_rate_limit("ref")
        enforcer.check_rate_limit("ref")
        with pytest.raises(MCPRateLimitExceededError):
            enforcer.check_rate_limit("ref")
        now[0] += 31  # refill ≈ 31 × (2/60) → > 1 token
        enforcer.check_rate_limit("ref")
    finally:
        monkeypatch.undo()


# ============================================================================
# API module (fonctions nommées par la tâche 11) + portail + intégration
# ============================================================================


def test_module_functions_accept_injected_enforcer() -> None:
    """Les fonctions module (tâche 11) délèguent à un ``enforcer`` explicite.

    L'état (quota, rate limit) vit dans l'enforceur : on réutilise LA MÊME
    instance pour vérifier le dépassement de débit.
    """
    scopes = {
        "m": _scope("m", role="admin", destructive_quota=1, rate_limit_per_minute=1)
    }
    enforcer = _enforcer(scopes)
    check_scope("m", "write_file", enforcer=enforcer)
    check_quota("m", "write_file", enforcer=enforcer)
    check_rate_limit("m", enforcer=enforcer)
    with pytest.raises(MCPRateLimitExceededError):
        check_rate_limit("m", enforcer=enforcer)


def test_enforce_runs_all_three_guards() -> None:
    """``enforce`` enchaîne scope → quota → rate limit (une garde suffit à bloquer)."""
    scopes = {
        "ok": _scope("ok", role="admin", destructive_quota=2, rate_limit_per_minute=5)
    }
    enforcer = _enforcer(scopes)
    enforcer.enforce("ok", "write_file")  # scope ✓ quota ✓ rate ✓
    with pytest.raises(MCPAccessDeniedError):
        enforcer.enforce("ghost", "write_file")  # client inconnu → bloque avant tout


def test_default_enforcer_is_shared_singleton() -> None:
    """L'enforceur partagé ne crée qu'une instance (aucune base au module import)."""
    reset_default_enforcer()
    try:
        first = get_default_enforcer()
        assert get_default_enforcer() is first
    finally:
        reset_default_enforcer()


def test_token_bucket_shared_with_rest_middleware() -> None:
    """Intégration tâche 11 : le middleware REST expose LA MÊME primitive TokenBucket.

    La primitive vit dans ``security/rate_limit_bucket.py`` ; le middleware
    ``api/middlewares/rate_limit.py`` l'importe (zéro duplication) tandis que
    l'enforceur n'importe jamais ``api``.
    """
    from api.middlewares import rate_limit as rest_rate_limit

    assert rest_rate_limit.TokenBucket is TokenBucket