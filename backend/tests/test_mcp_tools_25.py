# project/tests/test_mcp_tools_25.py
"""Tests d'acceptation — Tâche 7 : 25 Tools (extension read-only, S3, v1.0.0).

Checklist (docs/mcp/IMPLEMENTATION_PLAN.md, tâche 7) :

    - les 25 tools read-only sont exposés par ``build_mcp_server()`` par défaut
      (bootstrap S1 ``mcp_version``/``server_info`` inclus → 27 au ``tools/list``) ;
    - chaque tool passe par ``decide_action()`` → ``AUTO_APPROVE`` (read-only) ;
    - annotations cohérentes : ``readOnlyHint: true`` / ``destructiveHint: false`` /
      ``idempotentHint: true`` + ``required_scope == READ_ONLY`` ;
    - la sélection ``V100_READ_ONLY_TOOLS`` couvre exactement les 25 tools
      (V010 : 13 + extension tâche 7 : 12) — cf. ``legacy_tool_provider.py``.

Aucun appel réseau ni dépendance lourde dans cette suite.
"""

from __future__ import annotations

import json

from app.domain.entities.mcp import MCPScopeRole
from app.domain.entities.plan import Action
from app.infrastructure.mcp.legacy_tool_provider import (
    V100_READ_ONLY_TOOLS,
    build_v100_read_only_provider,
)
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.policy_adapter import decide_action


def test_selection_v100_is_25_read_only_tools() -> None:
    """La checklist v1.0.0 couvre exactement les 25 tools nommés par la tâche 7."""
    names = {tool.name for tool in build_v100_read_only_provider().list_tools()}
    assert len(names) == 25
    assert names == set(V100_READ_ONLY_TOOLS)


def test_default_server_exposes_25_plus_bootstrap() -> None:
    """``tools/list`` par défaut = 25 read-only + 2 bootstrap = 27 tools."""
    server = build_mcp_server()
    reply = json.loads(
        server.handle_text(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        )
    )
    names = {tool["name"] for tool in reply["result"]["tools"]}
    assert len(names) == 27
    assert {"mcp_version", "server_info"} <= names
    assert names >= set(V100_READ_ONLY_TOOLS)


def test_every_tool_is_read_only_with_coherent_annotations() -> None:
    """Annotations cohérentes : readOnlyHint true, scope READ_ONLY pour les 25."""
    for tool in build_v100_read_only_provider().list_tools():
        assert tool.annotations == {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
        }, tool.name
        assert tool.required_scope is MCPScopeRole.READ_ONLY, tool.name


def test_every_tool_passes_decide_action_as_auto_approve() -> None:
    """``decide_action(Action(tool=…))`` → AUTO_APPROVE pour chaque tool (read-only)."""

    from app.agent.policies.sandbox_policy import classify_tool

    for tool in build_v100_read_only_provider().list_tools():
        action = Action(tool=tool.name, args={}, category=classify_tool(tool.name))
        verdict = decide_action(action)
        assert verdict.decision.name == "AUTO_APPROVE", tool.name
        assert verdict.annotations["readOnlyHint"] is True, tool.name


def test_read_only_scope_sees_all_25_tools() -> None:
    """Un client ``read_only`` voit toute la surface v1.0.0 (filtre fail-closed)."""
    server = build_mcp_server(scope=MCPScopeRole.READ_ONLY)
    reply = json.loads(
        server.handle_text(
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        )
    )
    names = {tool["name"] for tool in reply["result"]["tools"]}
    assert names >= set(V100_READ_ONLY_TOOLS)