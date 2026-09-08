# project/tests/test_mcp_policy_adapter.py
"""Tests du policy adapter MCP (S2 — Tâche 5, docs/mcp/IMPLEMENTATION_PLAN.md).

Couverture :
    1. ``decision_to_annotations`` : Decision → readOnlyHint/destructiveHint/
       idempotentHint (fail-closed, dict fraîches, postures = manifeste) ;
    2. ``decide`` / ``decide_action`` : délégation stricte à ``sandbox_policy``
       (parité de verdict), prédicats allowed/requires_approval/blocked, raisons
       auditées (cible sensible, anti-SSRF, règle dure) ;
    3. ``PolicyVerdict`` : immuable + ``to_dict`` sérialisable (audit MCP) ;
    4. ``visible_tools`` : filtre par rôle cumulatif fail-closed, whitelist
       = intersection (S4 ``MCPSecurityScope``), ordre préservé ;
    5. ``ScopeFilteredToolProvider`` / ``PolicyGateToolProvider`` : contrats de
       port (``isinstance`` runtime_checkable), projection sécurisée de
       ``tools/list``, gate de ``tools/call`` (auto → exécution, approve →
       validation humaine, reject → refus), composition gate ∘ scope ;
    6. intégration ``MCPServer`` : ``tools/call`` de bout en bout (isError).

Aucun import lourd (ni torch, ni transformers) — le socle MCP est pur.
"""

from __future__ import annotations

import json
import logging
from dataclasses import FrozenInstanceError

import pytest

from app.agent.policies.sandbox_policy import decide as sandbox_decide
from app.agent.policies.sandbox_policy import decide_action as sandbox_decide_action
from app.domain.entities.mcp import MCPScopeRole, MCPTool, MCPVersion
from app.domain.entities.plan import Action, ActionCategory, Decision
from app.domain.ports.mcp_ports import MCPToolRegistryPort
from app.infrastructure.mcp.manifest_generator import (
    MUTATING_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
)
from app.infrastructure.mcp.mcp_server import (
    InMemoryToolProvider,
    MCPServer,
    ToolError,
)
from app.infrastructure.mcp.policy_adapter import (
    PolicyGateToolProvider,
    PolicyVerdict,
    ScopeFilteredToolProvider,
    decide,
    decide_action,
    decision_to_annotations,
    visible_tools,
)
from app.infrastructure.mcp.protocol import ErrorCode, empty_input_schema

_POLICY_LOGGER = "thinktuning.mcp.policy"


def _tool(
    name: str,
    *,
    scope: MCPScopeRole,
    annotations: dict[str, bool] | None = None,
) -> MCPTool:
    """Tool MCP minimal dont le handler confirme l'exécution effective."""

    def _handler(_arguments: dict) -> str:
        return f"ok:{name}"

    return MCPTool(
        name=name,
        description=f"tool de test {name}",
        input_schema=empty_input_schema(),
        annotations=annotations or dict(READ_ONLY_ANNOTATIONS),
        required_scope=scope,
        handler=_handler,
    )


@pytest.fixture()
def catalogue() -> list[MCPTool]:
    """Un tool par rôle de scope (read_only < contributor < operator < admin)."""
    return [
        _tool("read_file", scope=MCPScopeRole.READ_ONLY),
        _tool("write_file", scope=MCPScopeRole.CONTRIBUTOR),
        _tool("run_command", scope=MCPScopeRole.OPERATOR),
        _tool("admin_tool", scope=MCPScopeRole.ADMIN),
    ]


@pytest.fixture()
def inner(catalogue: list[MCPTool]) -> InMemoryToolProvider:
    return InMemoryToolProvider(catalogue)


# --- 1. decision_to_annotations : mapping verdict → annotations MCP ------------


def test_auto_approve_maps_to_read_only_annotations() -> None:
    assert decision_to_annotations(Decision.AUTO_APPROVE) == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    }


@pytest.mark.parametrize("decision", [Decision.APPROVE, Decision.REJECT])
def test_risky_decisions_map_to_mutating_annotations(decision: Decision) -> None:
    """APPROVE et REJECT → posture mutation fail-closed (hints ≠ permissions)."""
    assert decision_to_annotations(decision) == {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
    }


@pytest.mark.parametrize("decision", list(Decision))
def test_annotations_have_exactly_the_three_mcp_hints(decision: Decision) -> None:
    assert set(decision_to_annotations(decision)) == {
        "readOnlyHint",
        "destructiveHint",
        "idempotentHint",
    }


def test_annotations_are_aligned_with_manifest_postures() -> None:
    """Une seule source de postures : les constantes du manifeste (tâche 4)."""
    assert decision_to_annotations(Decision.AUTO_APPROVE) == READ_ONLY_ANNOTATIONS
    assert decision_to_annotations(Decision.APPROVE) == MUTATING_ANNOTATIONS
    assert decision_to_annotations(Decision.REJECT) == MUTATING_ANNOTATIONS


def test_annotations_are_fresh_dicts_not_the_constants() -> None:
    first = decision_to_annotations(Decision.APPROVE)
    second = decision_to_annotations(Decision.APPROVE)
    assert first is not second
    first["readOnlyHint"] = True  # tentative de falsification
    assert MUTATING_ANNOTATIONS["readOnlyHint"] is False


# --- 2. decide / decide_action : délégation stricte à sandbox_policy -----------

# Grille (tool, args, verdict attendu) — miroir des règles de sandbox_policy.
_CASES: list[tuple[str, dict, Decision]] = [
    ("read_file", {"path": "data/x.csv"}, Decision.AUTO_APPROVE),
    ("list_dir", {"path": "data"}, Decision.AUTO_APPROVE),
    ("git_status", {}, Decision.AUTO_APPROVE),
    ("http_get", {"url": "https://api.exemple.com/x"}, Decision.AUTO_APPROVE),
    ("write_file", {"path": "outputs/x.txt"}, Decision.APPROVE),
    ("remove_path", {"path": "outputs/x.txt"}, Decision.APPROVE),
    ("run_command", {"command": ["git", "--version"]}, Decision.APPROVE),
    ("outil_bizarre", {"x": 1}, Decision.APPROVE),  # UNKNOWN : prudence
    ("write_file", {"path": ".env"}, Decision.REJECT),
    ("remove_path", {"path": ".git/config"}, Decision.REJECT),
    ("sqlite_query", {"query": "DELETE FROM t"}, Decision.REJECT),
    ("http_post", {"url": "http://127.0.0.1:8000/run"}, Decision.REJECT),
]


@pytest.mark.parametrize(("tool", "args", "expected"), _CASES)
def test_decide_matches_sandbox_policy_verdict(
    tool: str, args: dict, expected: Decision
) -> None:
    """Parité stricte : le verdict de l'adapter = verdict sandbox_policy."""
    assert decide(tool, args).decision is expected
    assert sandbox_decide(tool, dict(args)) is expected


@pytest.mark.parametrize(("tool", "args", "expected"), _CASES)
def test_decide_annotations_follow_the_verdict(
    tool: str, args: dict, expected: Decision
) -> None:
    verdict = decide(tool, args)
    expected_posture = (
        READ_ONLY_ANNOTATIONS if expected is Decision.AUTO_APPROVE else MUTATING_ANNOTATIONS
    )
    assert verdict.annotations == expected_posture


def test_reads_are_allowed_without_approval() -> None:
    verdict = decide("read_file", {"path": "data/x.csv"})
    assert verdict.allowed
    assert not verdict.requires_approval
    assert not verdict.blocked


def test_writes_require_human_approval() -> None:
    verdict = decide("write_file", {"path": "outputs/x.txt"})
    assert verdict.requires_approval
    assert not verdict.allowed
    assert not verdict.blocked


def test_sensitive_path_is_blocked_with_audited_reason() -> None:
    verdict = decide("write_file", {"path": "certs/server.key"})
    assert verdict.blocked
    assert "cible sensible" in verdict.reason
    assert "server.key" in verdict.reason


def test_private_host_post_is_blocked_with_anti_ssrf_reason() -> None:
    verdict = decide("http_post", {"url": "http://192.168.1.5/run"})
    assert verdict.blocked
    assert "anti-SSRF" in verdict.reason


def test_mutating_sql_is_blocked_by_hard_rule() -> None:
    verdict = decide("postgres_query", {"query": "DROP TABLE users"})
    assert verdict.blocked
    assert "règle dure" in verdict.reason


def test_none_args_are_treated_as_empty() -> None:
    assert decide("git_status", None).decision is Decision.AUTO_APPROVE


def test_category_override_is_delegated() -> None:
    """Catégorie explicite : même sémantique que sandbox_policy.decide."""
    assert decide("mystere", {}, category=ActionCategory.READ).decision is Decision.AUTO_APPROVE
    assert (
        decide("mystere", {}, category=ActionCategory.WRITE).decision is Decision.APPROVE
    )


def test_decide_action_entity_mirrors_sandbox_policy() -> None:
    action = Action(tool="write_file", args={"path": ".env"}, category=ActionCategory.WRITE)
    verdict = decide_action(action)
    assert verdict.decision is sandbox_decide_action(action) is Decision.REJECT
    assert verdict.tool == "write_file"


def test_decide_action_default_category_is_unknown() -> None:
    action = Action(tool="write_file", args={"path": "outputs/x.txt"})
    assert decide_action(action).decision is Decision.APPROVE


def test_decide_is_deterministic() -> None:
    assert decide("write_file", {"path": "outputs/x.txt"}) == decide(
        "write_file", {"path": "outputs/x.txt"}
    )


# --- 3. PolicyVerdict : immuabilité + audit ------------------------------------


def test_verdict_is_immutable() -> None:
    verdict = decide("read_file", {"path": "a.txt"})
    with pytest.raises(FrozenInstanceError):
        verdict.tool = "other"  # type: ignore[misc]


def test_verdict_to_dict_shape_for_audit() -> None:
    verdict = decide("write_file", {"path": ".env"})
    payload = verdict.to_dict()
    assert payload == {
        "tool": "write_file",
        "decision": "reject",
        "allowed": False,
        "requires_approval": False,
        "blocked": True,
        "annotations": verdict.annotations,
        "reason": verdict.reason,
    }
    json.dumps(payload)  # sérialisable pour l'audit MCP (tâche 12)


def test_verdict_is_a_policy_verdict_instance() -> None:
    assert isinstance(decide("read_file", {}), PolicyVerdict)


# --- 4. visible_tools : filtre de scope fail-closed ----------------------------


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (MCPScopeRole.READ_ONLY, ["read_file"]),
        (MCPScopeRole.CONTRIBUTOR, ["read_file", "write_file"]),
        (MCPScopeRole.OPERATOR, ["read_file", "write_file", "run_command"]),
        (MCPScopeRole.ADMIN, ["read_file", "write_file", "run_command", "admin_tool"]),
    ],
)
def test_role_filter_is_cumulative(
    catalogue: list[MCPTool], scope: MCPScopeRole, expected: list[str]
) -> None:
    assert [tool.name for tool in visible_tools(catalogue, scope)] == expected


def test_whitelist_is_an_intersection_not_a_union(catalogue: list[MCPTool]) -> None:
    """Whitelist admin : soustractive, jamais additive."""
    assert [
        tool.name
        for tool in visible_tools(catalogue, MCPScopeRole.ADMIN, whitelist={"read_file"})
    ] == ["read_file"]


def test_role_remains_the_ceiling_even_with_a_broad_whitelist(
    catalogue: list[MCPTool],
) -> None:
    """Un tool whitelisté mais hors rôle reste invisible (le rôle plafonne)."""
    visible = visible_tools(
        catalogue,
        MCPScopeRole.READ_ONLY,
        whitelist={"read_file", "write_file", "run_command", "admin_tool"},
    )
    assert [tool.name for tool in visible] == ["read_file"]


def test_whitelist_excluding_a_granted_tool_hides_it(
    catalogue: list[MCPTool],
) -> None:
    """Intersection stricte : read_file accordé par le rôle, retiré par la whitelist."""
    visible = visible_tools(
        catalogue,
        MCPScopeRole.READ_ONLY,
        whitelist={"write_file", "run_command", "admin_tool"},
    )
    assert visible == []


def test_whitelist_with_unknown_names_is_tolerant_and_logged(
    catalogue: list[MCPTool], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger=_POLICY_LOGGER):
        visible = visible_tools(
            catalogue, MCPScopeRole.ADMIN, whitelist={"read_file", "ghost"}
        )
    assert [tool.name for tool in visible] == ["read_file"]
    assert any("ghost" in record.getMessage() for record in caplog.records)


def test_scope_accepts_a_plain_string_role(catalogue: list[MCPTool]) -> None:
    assert [tool.name for tool in visible_tools(catalogue, "read_only")] == ["read_file"]


def test_unknown_role_fails_closed(catalogue: list[MCPTool]) -> None:
    with pytest.raises(ValueError):
        visible_tools(catalogue, "root_god_mode")


def test_returns_a_new_list_never_the_catalogue(catalogue: list[MCPTool]) -> None:
    result = visible_tools(catalogue, MCPScopeRole.ADMIN)
    assert result == catalogue
    assert result is not catalogue


def test_input_order_is_preserved(catalogue: list[MCPTool]) -> None:
    shuffled = [catalogue[2], catalogue[0], catalogue[1], catalogue[3]]
    result = visible_tools(shuffled, MCPScopeRole.ADMIN)
    assert [tool.name for tool in result] == [
        "run_command",
        "read_file",
        "write_file",
        "admin_tool",
    ]


def test_empty_catalogue_yields_empty_view() -> None:
    assert visible_tools([], MCPScopeRole.ADMIN) == []


# --- 5. Providers : contrats de port + gate tools/call -------------------------


def test_scope_filtered_provider_satisfies_the_port(inner: InMemoryToolProvider) -> None:
    provider = ScopeFilteredToolProvider(inner, scope=MCPScopeRole.READ_ONLY)
    assert isinstance(provider, MCPToolRegistryPort)


def test_policy_gate_provider_satisfies_the_port(inner: InMemoryToolProvider) -> None:
    assert isinstance(PolicyGateToolProvider(inner), MCPToolRegistryPort)


def test_scope_provider_filters_list_tools(inner: InMemoryToolProvider) -> None:
    provider = ScopeFilteredToolProvider(inner, scope=MCPScopeRole.READ_ONLY)
    assert [tool.name for tool in provider.list_tools()] == ["read_file"]


def test_scope_provider_supports_the_s4_whitelist(inner: InMemoryToolProvider) -> None:
    provider = ScopeFilteredToolProvider(
        inner, scope=MCPScopeRole.ADMIN, whitelist={"read_file", "run_command"}
    )
    assert [tool.name for tool in provider.list_tools()] == ["read_file", "run_command"]


def test_scope_provider_call_invisible_tool_fails_closed(
    inner: InMemoryToolProvider,
) -> None:
    provider = ScopeFilteredToolProvider(inner, scope=MCPScopeRole.READ_ONLY)
    with pytest.raises(ToolError, match="Unknown tool: write_file"):
        provider.call_tool("write_file", {"path": "outputs/x.txt"})


def test_scope_provider_call_visible_tool_delegates(inner: InMemoryToolProvider) -> None:
    provider = ScopeFilteredToolProvider(inner, scope=MCPScopeRole.READ_ONLY)
    assert provider.call_tool("read_file", {"path": "data/x.csv"}) == "ok:read_file"


def test_gate_executes_auto_approved_calls(inner: InMemoryToolProvider) -> None:
    gate = PolicyGateToolProvider(inner)
    assert gate.call_tool("read_file", {"path": "data/x.csv"}) == "ok:read_file"


def test_gate_logs_the_verdict_for_audit(
    inner: InMemoryToolProvider, caplog: pytest.LogCaptureFixture
) -> None:
    gate = PolicyGateToolProvider(inner)
    with caplog.at_level(logging.INFO, logger=_POLICY_LOGGER):
        gate.call_tool("read_file", {"path": "a.txt"})
    assert any("auto-approuvé" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    ("tool", "args", "message"),
    [
        ("write_file", {"path": "outputs/x.txt"}, "Manual approval required"),
        ("run_command", {"command": ["git", "--version"]}, "Manual approval required"),
        ("write_file", {"path": ".env"}, "Policy rejected"),
        ("http_post", {"url": "http://127.0.0.1:8000/run"}, "Policy rejected"),
    ],
)
def test_gate_blocks_non_auto_verdicts(tool: str, args: dict, message: str) -> None:
    """Verdicts non-auto : le gate refuse AVANT toute exécution (ToolError)."""
    catalog = [
        _tool("read_file", scope=MCPScopeRole.READ_ONLY),
        _tool("write_file", scope=MCPScopeRole.CONTRIBUTOR),
        _tool("run_command", scope=MCPScopeRole.OPERATOR),
        _tool("http_post", scope=MCPScopeRole.OPERATOR),
    ]
    gate = PolicyGateToolProvider(InMemoryToolProvider(catalog))
    with pytest.raises(ToolError, match=message):
        gate.call_tool(tool, args)


def test_gate_never_executes_a_blocked_call() -> None:
    """Le handler d'un tool bloqué n'est JAMAIS invoqué (règle dure)."""
    executed: list[str] = []

    def _spy(_arguments: dict) -> str:
        executed.append("write_file")
        return "exécuté"

    catalog = [
        MCPTool(
            name="write_file",
            description="tool espion",
            input_schema=empty_input_schema(),
            annotations=dict(MUTATING_ANNOTATIONS),
            required_scope=MCPScopeRole.ADMIN,
            handler=_spy,
        )
    ]
    gate = PolicyGateToolProvider(InMemoryToolProvider(catalog))
    with pytest.raises(ToolError, match="Policy rejected"):
        gate.call_tool("write_file", {"path": ".env"})
    assert executed == []


def test_gate_unknown_tool_stays_unknown(inner: InMemoryToolProvider) -> None:
    gate = PolicyGateToolProvider(inner)
    with pytest.raises(ToolError, match="Unknown tool: ghost"):
        gate.call_tool("ghost", {})


def test_gate_keeps_list_tools_truthful(inner: InMemoryToolProvider) -> None:
    """La gate borne l'exécution, pas la visibilité (list_tools inchangé)."""
    assert [tool.name for tool in PolicyGateToolProvider(inner).list_tools()] == [
        "read_file",
        "write_file",
        "run_command",
        "admin_tool",
    ]


def test_gate_and_scope_compose(inner: InMemoryToolProvider) -> None:
    """Câblage cible : gate ∘ scope (l'invisibilité prime sur la policy)."""
    gated = PolicyGateToolProvider(
        ScopeFilteredToolProvider(inner, scope=MCPScopeRole.READ_ONLY)
    )
    with pytest.raises(ToolError, match="Unknown tool"):
        gated.call_tool("run_command", {"command": ["ls"]})
    assert gated.call_tool("read_file", {"path": "a.txt"}) == "ok:read_file"


# --- 6. Intégration MCPServer : tools/call de bout en bout ----------------------


def _rpc(method: str, params: dict, request_id: int = 1) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})


def _build_server(catalogue: list[MCPTool], scope: MCPScopeRole) -> MCPServer:
    """Serveur MCP câblé avec le policy adapter complet (gate ∘ scope)."""
    provider = PolicyGateToolProvider(
        ScopeFilteredToolProvider(InMemoryToolProvider(catalogue), scope=scope)
    )
    return MCPServer(
        version=MCPVersion(major=0, minor=1, patch=0),
        scope=scope,
        tool_provider=provider,
    )


def test_server_lists_only_visible_tools(catalogue: list[MCPTool]) -> None:
    server = _build_server(catalogue, MCPScopeRole.READ_ONLY)
    response = json.loads(server.handle_text(_rpc("tools/list", {})))
    tools = response["result"]["tools"]
    assert [tool["name"] for tool in tools] == ["read_file"]
    assert tools[0]["annotations"] == dict(READ_ONLY_ANNOTATIONS)


def test_server_read_call_roundtrip(catalogue: list[MCPTool]) -> None:
    server = _build_server(catalogue, MCPScopeRole.READ_ONLY)
    response = json.loads(
        server.handle_text(
            _rpc("tools/call", {"name": "read_file", "arguments": {"path": "a.txt"}})
        )
    )
    assert response["result"]["isError"] is False
    assert response["result"]["content"][0]["text"] == "ok:read_file"


def test_server_visible_mutating_call_is_gate_blocked(catalogue: list[MCPTool]) -> None:
    """Tool visible (scope contributor) mais verdict APPROVE → isError (gate)."""
    server = _build_server(catalogue, MCPScopeRole.CONTRIBUTOR)
    response = json.loads(
        server.handle_text(
            _rpc("tools/call", {"name": "write_file", "arguments": {"path": "outputs/x.txt"}})
        )
    )
    assert response["result"]["isError"] is True
    assert "Manual approval required" in response["result"]["content"][0]["text"]


def test_server_hard_rejected_call_is_gate_blocked(catalogue: list[MCPTool]) -> None:
    server = _build_server(catalogue, MCPScopeRole.CONTRIBUTOR)
    response = json.loads(
        server.handle_text(
            _rpc("tools/call", {"name": "write_file", "arguments": {"path": ".env"}})
        )
    )
    assert response["result"]["isError"] is True
    assert "Policy rejected" in response["result"]["content"][0]["text"]


def test_server_invisible_call_is_unknown_tool(catalogue: list[MCPTool]) -> None:
    server = _build_server(catalogue, MCPScopeRole.READ_ONLY)
    response = json.loads(
        server.handle_text(_rpc("tools/call", {"name": "write_file", "arguments": {}}))
    )
    # Erreur de protocole : un tool invisible est un tool absent (aucun oracle).
    assert response["error"]["code"] == ErrorCode.INVALID_PARAMS
