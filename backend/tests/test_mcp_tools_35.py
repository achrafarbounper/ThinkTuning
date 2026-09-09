# project/tests/test_mcp_tools_35.py
"""Tests d'acceptation — Tâche 17 : 35 Tools (extension write/exec, S6, v2.1.0).

Checklist (docs/mcp/IMPLEMENTATION_PLAN.md, tâche 17) :

    - ``V210_WRITE_EXEC_TOOLS`` couvre exactement les 10 tools write/exec de la
      checklist (write_file, write_json, append_file, make_dir, copy_path,
      run_command, run_python, start_training, cancel_training, stop_training) ;
    - chaque tool compile en posture MUTATION (``readOnlyHint: false`` /
      ``destructiveHint: true`` / ``idempotentHint: false``) avec le scope
      ``OPERATOR`` (catalogue par rôle = 35 tools) ;
    - chaque tool passe par ``decide_action()`` → ``APPROVE`` (validation
      humaine obligatoire) ; les cibles sensibles (``.git``/``.env``…)
      → ``REJECT`` (règle dure, jamais exécutée) ;
    - l'union read-only (25, tâche 7) + write/exec (10) = **35 tools** — les
      deux sélections sont disjointes par construction (fail-closed inversé :
      le provider read-only n'expose que de la lecture, le provider write/exec
      n'expose que de la mutation) ;
    - ``build_mcp_server`` : extension gated par version (>= 2.1.0) et par
      scope (OPERATOR) ; le ``PolicyGateToolProvider`` exige l'approbation
      humaine pour les mutations et rejette les cibles sensibles.

Aucun appel réseau ni dépendance lourde dans cette suite — et aucune écriture
réelle : les mutations sont bloquées par la policy AVANT tout handler.
"""

from __future__ import annotations

import json

import pytest

from app.agent.policies.sandbox_policy import classify_tool
from app.domain.entities.mcp import MCPVersion, MCPScopeRole
from app.domain.entities.plan import Action
from app.infrastructure.mcp.legacy_tool_provider import (
    V100_READ_ONLY_TOOLS,
    build_v100_read_only_provider,
)
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.policy_adapter import decide_action
from app.infrastructure.mcp.write_exec_tool_provider import (
    V210_WRITE_EXEC_TOOLS,
    WriteExecToolProvider,
    build_v210_write_exec_provider,
)

V100 = MCPVersion(major=1, minor=0, patch=0)
V200 = MCPVersion(major=2, minor=0, patch=0)
V210 = MCPVersion(major=2, minor=1, patch=0)


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


# --- Sélection & provider (tâche 17) -----------------------------------------


def test_selection_v210_is_the_10_checklist_tools() -> None:
    """La checklist v2.1.0 couvre exactement les 10 tools nommés par la tâche 17."""
    assert V210_WRITE_EXEC_TOOLS == frozenset(
        {
            "write_file",
            "write_json",
            "append_file",
            "make_dir",
            "copy_path",
            "run_command",
            "run_python",
            "start_training",
            "cancel_training",
            "stop_training",
        }
    )


def test_provider_exposes_exactly_the_selection() -> None:
    """Le provider expose les 10 tools de la sélection (et rien d'autre)."""
    names = {tool.name for tool in build_v210_write_exec_provider().list_tools()}
    assert len(names) == 10
    assert names == set(V210_WRITE_EXEC_TOOLS)


def test_every_tool_has_mutation_annotations_and_operator_scope() -> None:
    """Annotations mutantes + scope OPERATOR pour les 10 tools (miroir tâche 7)."""
    for tool in build_v210_write_exec_provider().list_tools():
        assert tool.annotations == {
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
        }, tool.name
        assert tool.required_scope is MCPScopeRole.OPERATOR, tool.name


def test_every_tool_passes_decide_action_as_approve() -> None:
    """``decide_action(Action(tool=…))`` → APPROVE pour chaque tool (mutation)."""
    for tool in build_v210_write_exec_provider().list_tools():
        action = Action(tool=tool.name, args={}, category=classify_tool(tool.name))
        verdict = decide_action(action)
        assert verdict.decision.name == "APPROVE", tool.name
        assert verdict.annotations["readOnlyHint"] is False, tool.name


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("write_file", {"filename": ".env", "content": "x=1"}),
        ("write_json", {"path": ".env", "data": {}}),
        ("append_file", {"path": ".env", "content": "x"}),
        ("make_dir", {"path": "secrets/.git"}),
        ("copy_path", {"src": "a.txt", "dst": ".git/config"}),
        ("run_command", {"command": ["ls"], "cwd": ".env"}),
        ("start_training", {"local_corrections_path": ".env/corrections.jsonl"}),
    ],
)
def test_sensitive_paths_are_rejected(tool: str, args: dict) -> None:
    """Règle dure : toute cible sensible (.git/.env…) → REJECT, jamais exécutée."""
    action = Action(tool=tool, args=args, category=classify_tool(tool))
    assert decide_action(action).decision.name == "REJECT"


def test_selections_are_disjoint_and_union_is_35() -> None:
    """Read-only (25) et write/exec (10) sont disjointes : union = 35 tools."""
    assert V100_READ_ONLY_TOOLS.isdisjoint(V210_WRITE_EXEC_TOOLS)
    assert len(V100_READ_ONLY_TOOLS | V210_WRITE_EXEC_TOOLS) == 35


# --- Fail-closed inversé (construction du provider) ---------------------------


def test_fail_closed_excludes_read_only_posture() -> None:
    """Un tool résolu en posture LECTURE est exclu de la surface write/exec.

    ``add`` figure dans le manifeste réel (safety « safe » déclarée) : demandé
    dans la sélection, il est écarté — la surface write/exec n'expose que de
    la mutation (le doute n'est jamais résolu côté client).
    """
    provider = WriteExecToolProvider(selection={"add", "write_file"})
    assert {tool.name for tool in provider.list_tools()} == {"write_file"}


def test_fail_closed_excludes_unknown_names() -> None:
    """Un nom absent du manifeste legacy est exclu (aucun stub synthétisé)."""
    provider = WriteExecToolProvider(selection={"write_file", "ghost_tool"})
    assert {tool.name for tool in provider.list_tools()} == {"write_file"}


def test_handler_delegates_to_injected_legacy_impl() -> None:
    """L'appel délègue à l'implémentation legacy injectée (mécanique tâche 6)."""
    calls: list[tuple[str, str]] = []

    def fake_write_file(filename: str, content: str, max_bytes: int | None = None) -> str:
        calls.append((filename, content))
        return f"wrote {filename}"

    provider = WriteExecToolProvider(
        selection={"write_file"}, tools={"write_file": fake_write_file}
    )
    out = provider.call_tool("write_file", {"filename": "a.txt", "content": "hi"})
    assert out == "wrote a.txt"
    assert calls == [("a.txt", "hi")]


# --- Surface serveur : gating version + scope ---------------------------------


def test_v100_surface_unchanged_and_ungated() -> None:
    """La surface v1.0.0 reste 27 tools, sans extension ni gate de policy."""
    names = _tools_list(V100, MCPScopeRole.OPERATOR)
    assert len(names) == 27
    assert V210_WRITE_EXEC_TOOLS.isdisjoint(names)
    result = _call(V100, MCPScopeRole.READ_ONLY, "mcp_version", {})
    assert result["isError"] is False
    assert result["content"][0]["text"] == "1.0.0"


@pytest.mark.parametrize(
    ("version", "scope"),
    [
        (V100, MCPScopeRole.OPERATOR),  # version < 2.1.0 : extension absente
        (V200, MCPScopeRole.OPERATOR),  # idem v2.0.0 (orchestrate seul, tâche 16)
        (V210, MCPScopeRole.READ_ONLY),  # scope insuffisant : filtre fail-closed
    ],
)
def test_write_exec_hidden(version: MCPVersion, scope: MCPScopeRole) -> None:
    """Aucun tool write/exec hors (>= 2.1.0, OPERATOR) — double barrière."""
    assert V210_WRITE_EXEC_TOOLS.isdisjoint(_tools_list(version, scope))


def test_v210_operator_exposes_union_35_plus_bootstrap_and_orchestrate() -> None:
    """Surface v2.1.0 OPERATOR : union 35 + 2 bootstrap + orchestrate = 38."""
    names = _tools_list(V210, MCPScopeRole.OPERATOR)
    assert (V100_READ_ONLY_TOOLS | V210_WRITE_EXEC_TOOLS) <= names
    assert len(names) == 38


# --- Gate de policy (PolicyGateToolProvider actif dès la v2.1.0) ---------------


def test_gate_auto_approves_reads_at_v210() -> None:
    """Lecture/introspection : AUTO_APPROVE → exécution directe (pas de blocage)."""
    result = _call(V210, MCPScopeRole.READ_ONLY, "mcp_version", {})
    assert result["isError"] is False
    assert result["content"][0]["text"] == "2.1.0"
    result = _call(V210, MCPScopeRole.OPERATOR, "add", {"a": 2, "b": 3})
    assert result["isError"] is False


def test_gate_requires_manual_approval_for_mutations() -> None:
    """Write/exec sur cible non sensible : APPROVE → validation humaine exigée."""
    for name, args in (
        ("write_file", {"filename": "outputs/report.txt", "content": "x"}),
        ("start_training", {}),
    ):
        result = _call(V210, MCPScopeRole.OPERATOR, name, args)
        assert result["isError"] is True, name
        assert "Manual approval required" in result["content"][0]["text"], name


def test_gate_rejects_sensitive_paths() -> None:
    """Règle dure côté serveur : cible sensible → REJECT (refus explicite)."""
    result = _call(
        V210,
        MCPScopeRole.OPERATOR,
        "write_file",
        {"filename": ".env", "content": "x=1"},
    )
    assert result["isError"] is True
    assert "Policy rejected" in result["content"][0]["text"]