# project/tests/test_mcp_tools_40.py
"""Tests d'acceptation — Tâche 19 : 40 Tools (full catalogue, S7, v2.2.0).

Checklist (docs/mcp/IMPLEMENTATION_PLAN.md, tâche 19) :

    - ``V220_ADMIN_TOOLS`` couvre exactement les 5 tools de la checklist
      (move_path, remove_path, split_file, dedupe_lines, unzip_file) ;
    - chaque tool compile en posture MUTATION (``readOnlyHint: false`` /
      ``destructiveHint: true`` / ``idempotentHint: false``) avec le scope
      ``ADMIN`` (catalogue par rôle = 40 tools) ;
    - chaque tool passe par ``decide_action()`` → ``APPROVE`` (validation
      humaine obligatoire) ; les cibles sensibles (``.git``/``.env``…)
      → ``REJECT`` (règle dure, jamais exécutée) ;
    - l'union read-only (25, tâche 7) + write/exec (10, tâche 17) + admin
      (5) = **40 tools** — les trois sélections sont disjointes par
      construction et couvrent ``ADMIN_ROLE_TOOLS`` (scope_enforcer, tâche 11) ;
    - ``MCPSecurityScope`` : la portée effective d'un client ``admin``
      (whitelist vide = catalogue du rôle) couvre les 40 tools ; un client
      ``operator`` reste hors portée ; la whitelist explicite est soustractive ;
    - ``build_mcp_server`` : extension gated par version (>= 2.2.0) et par
      scope (ADMIN) ; le ``PolicyGateToolProvider`` exige l'approbation
      humaine pour les mutations et rejette les cibles sensibles.

Aucun appel réseau ni dépendance lourde dans cette suite — et aucune écriture
réelle : les mutations sont bloquées par la policy AVANT tout handler.
"""

from __future__ import annotations

import json

import pytest

from app.agent.policies.sandbox_policy import classify_tool
from app.domain.entities.mcp import MCPScopeRole, MCPVersion
from app.domain.entities.plan import Action
from app.domain.ports.mcp_ports import MCPSecurityScope
from app.infrastructure.mcp.admin_tool_provider import (
    ADMIN_TOOLS_SCOPE,
    V220_ADMIN_TOOLS,
    AdminToolProvider,
    build_v220_admin_provider,
)
from app.infrastructure.mcp.legacy_tool_provider import V100_READ_ONLY_TOOLS
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.policy_adapter import decide_action, visible_tools
from app.infrastructure.mcp.security.scope_enforcer import (
    ADMIN_ROLE_TOOLS,
    OPERATOR_ROLE_TOOLS,
    ROLE_TOOLS,
    effective_tools,
)
from app.infrastructure.mcp.write_exec_tool_provider import V210_WRITE_EXEC_TOOLS

V210 = MCPVersion(major=2, minor=1, patch=0)
V220 = MCPVersion(major=2, minor=2, patch=0)


def _tools_list(version: MCPVersion, scope: MCPScopeRole) -> set[str]:
    """Noms exposés par ``tools/list`` pour une surface (version, scope)."""
    server = build_mcp_server(version=version, scope=scope)
    reply = json.loads(
        server.handle_text(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        )
    )
    return {tool["name"] for tool in reply["result"]["tools"]}


def _call(
    version: MCPVersion, scope: MCPScopeRole, name: str, arguments: dict
) -> dict:
    """``tools/call`` brut (résultat MCP, erreurs métier incluses)."""
    server = build_mcp_server(version=version, scope=scope)
    reply = json.loads(
        server.handle_text(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": arguments},
                }
            )
        )
    )
    return reply["result"]


# --- Sélection & provider (tâche 19) -----------------------------------------


def test_selection_v220_is_the_5_checklist_tools() -> None:
    """La checklist v2.2.0 couvre exactement les 5 tools nommés par la tâche 19."""
    assert V220_ADMIN_TOOLS == frozenset(
        {
            "move_path",
            "remove_path",
            "split_file",
            "dedupe_lines",
            "unzip_file",
        }
    )


def test_provider_exposes_exactly_the_selection() -> None:
    """Le provider expose les 5 tools de la sélection (et rien d'autre)."""
    names = {tool.name for tool in build_v220_admin_provider().list_tools()}
    assert len(names) == 5
    assert names == set(V220_ADMIN_TOOLS)


def test_every_tool_has_mutation_annotations_and_admin_scope() -> None:
    """Annotations mutantes + scope ADMIN pour les 5 tools (miroir tâche 17)."""
    for tool in build_v220_admin_provider().list_tools():
        assert tool.annotations == {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
        }, tool.name
        assert tool.required_scope is MCPScopeRole.ADMIN, tool.name
        assert tool.required_scope is ADMIN_TOOLS_SCOPE, tool.name


def test_every_tool_passes_decide_action_as_approve() -> None:
    """``decide_action(Action(tool=…))`` → APPROVE pour chaque tool (mutation)."""
    for tool in build_v220_admin_provider().list_tools():
        action = Action(tool=tool.name, args={}, category=classify_tool(tool.name))
        verdict = decide_action(action)
        assert verdict.decision.name == "APPROVE", tool.name
        assert verdict.annotations["readOnlyHint"] is False, tool.name


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("move_path", {"src": ".env", "dst": "ok.txt"}),
        ("move_path", {"src": "a.txt", "dst": ".git/config"}),
        ("remove_path", {"path": ".env"}),
        ("remove_path", {"path": "backup/id_rsa"}),
        ("split_file", {"path": "secrets/.env"}),
        ("dedupe_lines", {"path": "cache/__pycache__/data.txt"}),
        ("unzip_file", {"src": "archive/.env", "dst": "out"}),
    ],
)
def test_sensitive_paths_are_rejected(tool: str, args: dict) -> None:
    """Règle dure : toute cible sensible (.git/.env/…/id_rsa) → REJECT, jamais exécutée."""
    action = Action(tool=tool, args=args, category=classify_tool(tool))
    assert decide_action(action).decision.name == "REJECT"


# --- Catalogue complet : 40 tools (25 + 10 + 5, disjointes) --------------------


def test_selections_are_disjoint_and_union_is_40() -> None:
    """Read-only (25) + write/exec (10) + admin (5) : disjointes, union = 40."""
    assert V100_READ_ONLY_TOOLS.isdisjoint(V210_WRITE_EXEC_TOOLS)
    assert V100_READ_ONLY_TOOLS.isdisjoint(V220_ADMIN_TOOLS)
    assert V210_WRITE_EXEC_TOOLS.isdisjoint(V220_ADMIN_TOOLS)
    full = V100_READ_ONLY_TOOLS | V210_WRITE_EXEC_TOOLS | V220_ADMIN_TOOLS
    assert len(full) == 40


def test_full_catalogue_matches_admin_role_catalogue() -> None:
    """L'union des trois sélections = ``ADMIN_ROLE_TOOLS`` (catalogue 40, tâche 11)."""
    full = V100_READ_ONLY_TOOLS | V210_WRITE_EXEC_TOOLS | V220_ADMIN_TOOLS
    assert full == ADMIN_ROLE_TOOLS
    assert len(ADMIN_ROLE_TOOLS) == 40
    assert len(OPERATOR_ROLE_TOOLS) == 35
    assert V220_ADMIN_TOOLS.isdisjoint(OPERATOR_ROLE_TOOLS)  # operator ⊂ admin
    assert ADMIN_ROLE_TOOLS == OPERATOR_ROLE_TOOLS | V220_ADMIN_TOOLS
    assert "remove_path" not in OPERATOR_ROLE_TOOLS  # DELETE : admin uniquement
    assert "unzip_file" not in OPERATOR_ROLE_TOOLS
    assert ROLE_TOOLS[MCPScopeRole.ADMIN.value] is ADMIN_ROLE_TOOLS


# --- Fail-closed inversé (construction du provider) ---------------------------


def test_fail_closed_excludes_read_only_posture() -> None:
    """Un tool résolu en posture LECTURE est exclu de la surface admin.

    ``add`` figure dans le manifeste réel (safety « safe » déclarée) : demandé
    dans la sélection, il est écarté — la surface admin n'expose que de la
    mutation (le doute n'est jamais résolu côté client).
    """
    provider = AdminToolProvider(selection={"add", "move_path"})
    assert {tool.name for tool in provider.list_tools()} == {"move_path"}


def test_fail_closed_excludes_unknown_names() -> None:
    """Un nom absent du manifeste legacy est exclu (aucun stub synthétisé)."""
    provider = AdminToolProvider(selection={"move_path", "ghost_tool"})
    assert {tool.name for tool in provider.list_tools()} == {"move_path"}


def test_handler_delegates_to_injected_legacy_impl() -> None:
    """L'appel délègue à l'implémentation legacy injectée (mécanique tâche 6)."""
    calls: list[tuple[str, str]] = []

    def fake_move_path(src: str, dst: str) -> str:
        calls.append((src, dst))
        return f"moved {src} -> {dst}"

    provider = AdminToolProvider(
        selection={"move_path"}, tools={"move_path": fake_move_path}
    )
    out = provider.call_tool("move_path", {"src": "a.txt", "dst": "b.txt"})
    assert out == "moved a.txt -> b.txt"
    assert calls == [("a.txt", "b.txt")]


# --- MCPSecurityScope : portée effective + whitelist (S4) ----------------------


def test_admin_scope_effective_tools_cover_the_full_catalogue() -> None:
    """Un scope ``admin`` (whitelist vide) : portée effective = 40 tools."""
    scope = MCPSecurityScope(client_id="admin-client", role="admin")
    assert effective_tools(scope) == ADMIN_ROLE_TOOLS
    assert V220_ADMIN_TOOLS <= effective_tools(scope)


def test_operator_scope_never_covers_the_admin_extension() -> None:
    """Un scope ``operator`` : 35 tools — aucun des 5 tools admin (fail-closed)."""
    scope = MCPSecurityScope(client_id="operator-client", role="operator")
    assert effective_tools(scope) == OPERATOR_ROLE_TOOLS
    assert effective_tools(scope).isdisjoint(V220_ADMIN_TOOLS)


def test_scope_whitelist_is_subtractive() -> None:
    """La whitelist ``visible_tools`` (S4) intersecte le catalogue, jamais l'étend."""
    scope = MCPSecurityScope(
        client_id="restricted-admin", role="admin", visible_tools=["move_path"]
    )
    assert effective_tools(scope) == {"move_path"}


def test_scope_rank_projection_of_the_admin_surface() -> None:
    """Projection ``visible_tools`` : les 5 tools ne sortent qu'en rôle ADMIN."""
    tools = build_v220_admin_provider().list_tools()
    assert len(visible_tools(tools, MCPScopeRole.ADMIN)) == 5
    for role in (
        MCPScopeRole.READ_ONLY,
        MCPScopeRole.CONTRIBUTOR,
        MCPScopeRole.OPERATOR,
    ):
        assert visible_tools(tools, role) == [], role
    assert MCPScopeRole.ADMIN.granted(ADMIN_TOOLS_SCOPE)
    assert not MCPScopeRole.OPERATOR.granted(ADMIN_TOOLS_SCOPE)


# --- Surface serveur : gating version + scope ---------------------------------


@pytest.mark.parametrize(
    ("version", "scope"),
    [
        (V210, MCPScopeRole.OPERATOR),  # version < 2.2.0 : extension absente
        (V210, MCPScopeRole.ADMIN),  # idem, même en rôle admin
        (V220, MCPScopeRole.READ_ONLY),  # scope insuffisant : filtre fail-closed
        (V220, MCPScopeRole.CONTRIBUTOR),  # idem contributor
        (V220, MCPScopeRole.OPERATOR),  # idem operator (35 tools, sans admin)
    ],
)
def test_admin_tools_hidden(version: MCPVersion, scope: MCPScopeRole) -> None:
    """Aucun tool admin hors (>= 2.2.0, ADMIN) — double barrière."""
    assert V220_ADMIN_TOOLS.isdisjoint(_tools_list(version, scope))


def test_v210_surface_unchanged() -> None:
    """La surface v2.1.0 OPERATOR reste 38 tools (aucun tool admin injecté)."""
    names = _tools_list(V210, MCPScopeRole.OPERATOR)
    assert len(names) == 38
    assert V220_ADMIN_TOOLS.isdisjoint(names)


def test_v220_admin_exposes_union_40_plus_bootstrap_and_orchestrate() -> None:
    """Surface v2.2.0 ADMIN : union 40 + 2 bootstrap + orchestrate = 43."""
    names = _tools_list(V220, MCPScopeRole.ADMIN)
    assert (V100_READ_ONLY_TOOLS | V210_WRITE_EXEC_TOOLS | V220_ADMIN_TOOLS) <= names
    assert len(names) == 43


# --- Gate de policy (PolicyGateToolProvider actif dès la v2.1.0) ---------------


def test_gate_auto_approves_reads_at_v220() -> None:
    """Lecture/introspection : AUTO_APPROVE → exécution directe (pas de blocage)."""
    result = _call(V220, MCPScopeRole.ADMIN, "mcp_version", {})
    assert result["isError"] is False
    assert result["content"][0]["text"] == "2.2.0"
    result = _call(V220, MCPScopeRole.ADMIN, "add", {"a": 2, "b": 3})
    assert result["isError"] is False


def test_gate_requires_manual_approval_for_mutations() -> None:
    """Admin sur cible non sensible : APPROVE → validation humaine exigée."""
    for name, args in (
        ("move_path", {"src": "a.txt", "dst": "b.txt"}),
        ("remove_path", {"path": "tmp/scratch.txt"}),
        ("split_file", {"path": "data/big.txt"}),
        ("dedupe_lines", {"path": "data/lines.txt"}),
        ("unzip_file", {"src": "bundle.zip", "dst": "out"}),
    ):
        result = _call(V220, MCPScopeRole.ADMIN, name, args)
        assert result["isError"] is True, name
        assert "Manual approval required" in result["content"][0]["text"], name


def test_gate_rejects_sensitive_paths() -> None:
    """Règle dure côté serveur : cible sensible → REJECT (refus explicite)."""
    result = _call(
        V220,
        MCPScopeRole.ADMIN,
        "remove_path",
        {"path": ".env"},
    )
    assert result["isError"] is True
    assert "Policy rejected" in result["content"][0]["text"]
