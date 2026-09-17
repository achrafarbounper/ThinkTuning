# project/tests/test_mcp_deprecation.py
"""Tests du retrait des tools MCP (MCP 2.3.0 — dépréciation).

Couverture :
    1. ``compile_tool``   : projection ``deprecated`` / ``deprecationMessage`` /
       ``sunsetAt`` depuis ``tools_config.json`` + warning de compilation ;
    2. ``build_manifest`` : compteur ``deprecatedCount`` (compat : tools
       non dépréciés → entrée inchangée hors champ ``deprecated: false``) ;
    3. ``entry_to_mcp_tool`` + ``MCPTool.to_dict`` : projection ``tools/list`` ;
    4. serveur MCP        : ``tools/call`` sur un tool déprécié → événement
       d'audit DÉDIÉ (``mcp_tool_deprecated``) + enrichissement du détail de
       l'appel normal ; tool non déprécié → détail INCHANGÉ (compat) ;
    5. rendu Markdown     : colonnes ``Deprecated`` / ``Sunset`` du catalogue.

Le socle MCP est self-contained (AUCUNE dépendance au SDK ``mcp``).
"""

from __future__ import annotations

import json

from app.domain.entities.mcp import MCPScopeRole, MCPTool, MCPVersion
from app.infrastructure.mcp.manifest_generator import (
    build_manifest,
    compile_tool,
    entry_to_mcp_tool,
    manifest_to_markdown,
)
from app.infrastructure.mcp.mcp_server import InMemoryToolProvider, MCPServer
from app.infrastructure.mcp.protocol import empty_input_schema

_FIXED_TS = "2026-09-17T00:00:00.000Z"


def _handler(_: dict) -> str:
    return "ok"


def _server(*tools: MCPTool, audit=None) -> MCPServer:
    return MCPServer(
        version=MCPVersion(major=2, minor=3, patch=0),
        scope=MCPScopeRole.ADMIN,
        tool_provider=InMemoryToolProvider(list(tools)),
        audit=audit,
    )


def _tool(**overrides) -> MCPTool:
    entry = {
        "name": "old_tool",
        "description": "Ancien",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"readOnlyHint": True},
        "requiredScope": "read_only",
        "deprecated": True,
        "deprecationMessage": "Utiliser new_tool",
        "sunsetAt": "2027-01-01",
    }
    entry.update(overrides)
    return entry_to_mcp_tool(entry, _handler)


# --- 1. compile_tool : projection des métadonnées de retrait -------------------


def test_compile_tool_non_deprecated_entry_is_unchanged() -> None:
    """Tool sans dépréciation : entrée standard, ``deprecated`` explicite faux."""
    entry, warnings = compile_tool("read_file", {"name": "read_file", "description": "Lit"})
    assert entry["deprecated"] is False
    assert "deprecationMessage" not in entry
    assert "sunsetAt" not in entry
    assert not any("DÉPRÉCIÉ" in w for w in warnings)


def test_compile_tool_deprecated_full_metadata() -> None:
    """Tool déprécié : les 3 métadonnées sont projetées + warning de compilation."""
    meta = {
        "name": "old_tool",
        "description": "Ancien tool",
        "deprecated": True,
        "deprecationMessage": "Utiliser new_tool",
        "sunsetAt": "2027-01-01",
    }
    entry, warnings = compile_tool("old_tool", meta)
    assert entry["deprecated"] is True
    assert entry["deprecationMessage"] == "Utiliser new_tool"
    assert entry["sunsetAt"] == "2027-01-01"
    assert any("old_tool" in w and "DÉPRÉCIÉ" in w for w in warnings)


def test_compile_tool_deprecated_without_optional_fields() -> None:
    """``deprecated: true`` seul : champs optionnels omis, warning sans date."""
    meta = {"name": "old_tool", "description": "Ancien tool", "deprecated": True}
    entry, warnings = compile_tool("old_tool", meta)
    assert entry["deprecated"] is True
    assert "deprecationMessage" not in entry

# --- 2. build_manifest : compteur de dépréciés ---------------------------------


def test_build_manifest_counts_deprecated() -> None:
    tools = {
        "old_tool": {
            "name": "old_tool",
            "description": "Ancien",
            "deprecated": True,
            "deprecationMessage": "Migration requise",
            "sunsetAt": "2027-06-01",
        },
        "fresh_tool": {"name": "fresh_tool", "description": "Récent"},
    }
    manifest = build_manifest(tools, generated_at=_FIXED_TS)
    assert manifest["deprecatedCount"] == 1
    by_name = {t["name"]: t for t in manifest["tools"]}
    assert by_name["old_tool"]["deprecated"] is True
    assert by_name["fresh_tool"]["deprecated"] is False


# --- 3. entry_to_mcp_tool + projection tools/list ------------------------------


def test_entry_to_mcp_tool_carries_deprecation() -> None:
    tool = _tool()
    assert tool.deprecated is True
    assert tool.deprecation_message == "Utiliser new_tool"
    assert tool.sunset_at == "2027-01-01"
    projected = tool.to_dict()
    assert projected["deprecated"] is True
    assert projected["deprecationMessage"] == "Utiliser new_tool"
    assert projected["sunsetAt"] == "2027-01-01"


def test_mcp_tool_defaults_are_backward_compatible() -> None:
    """MCPTool construit sans champs de retrait : projection stable et fausse."""
    tool = MCPTool(
        name="plain",
        description="Tool classique",
        input_schema=empty_input_schema(),
        annotations={"readOnlyHint": True},
        required_scope=MCPScopeRole.READ_ONLY,
        handler=_handler,
    )
    assert tool.deprecated is False
    assert tool.to_dict()["deprecated"] is False


# --- 4. serveur : audit de l'usage d'un tool déprécié --------------------------


def test_tools_call_on_deprecated_tool_emits_dedicated_audit_event() -> None:
    events: list[dict] = []
    server = _server(_tool(), audit=lambda action, **kw: events.append({"action": action, **kw}))
    reply = json.loads(
        server.handle_text(
            json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "old_tool", "arguments": {}}}
            )
        )
    )
    # L'appel PROCEDE (compatibilité : pas de rupture pour les clients).
    assert "error" not in reply
    assert reply["result"]["isError"] is False
    deprecation_events = [e for e in events if e["action"] == "mcp_tool_deprecated"]
    assert deprecation_events, f"événement dédié absent : {[e['action'] for e in events]}"
    detail = deprecation_events[0]["detail"]
    assert detail["deprecated"] is True
    assert detail["deprecationMessage"] == "Utiliser new_tool"
    assert detail["sunsetAt"] == "2027-01-01"
    # L'appel normal est lui aussi enrichi (traçabilité en un coup d'œil).
    normal = [e for e in events if e["action"] == "mcp_tool_call"][0]
    assert normal["detail"]["deprecated"] is True


def test_tools_call_on_regular_tool_audit_detail_unchanged() -> None:
    """Tool NON déprécié : aucun champ de retrait dans le détail (compat)."""
    tool = MCPTool(
        name="plain",
        description="Tool classique",
        input_schema=empty_input_schema(),
        annotations={"readOnlyHint": True},
        required_scope=MCPScopeRole.READ_ONLY,
        handler=_handler,
    )
    events: list[dict] = []
    server = _server(tool, audit=lambda action, **kw: events.append({"action": action, **kw}))
    server.handle_text(
        json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "plain", "arguments": {}}}
        )
    )
    assert all(e["action"] != "mcp_tool_deprecated" for e in events)
    normal = [e for e in events if e["action"] == "mcp_tool_call"][0]
    assert "deprecated" not in normal["detail"]


def test_unknown_tool_audit_detail_unchanged() -> None:
    """Tool inconnu : pas d'événement de dépréciation, détail inchangé."""
    tool = MCPTool(
        name="plain",
        description="Tool classique",
        input_schema=empty_input_schema(),
        annotations={"readOnlyHint": True},
        required_scope=MCPScopeRole.READ_ONLY,
        handler=_handler,
    )
    events: list[dict] = []
    server = _server(tool, audit=lambda action, **kw: events.append({"action": action, **kw}))
    server.handle_text(
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ghost"}})
    )
    assert all(e["action"] != "mcp_tool_deprecated" for e in events)


# --- 5. rendu Markdown du catalogue -------------------------------------------


def test_manifest_markdown_renders_deprecation_columns() -> None:
    manifest = build_manifest(
        {
            "old_tool": {
                "name": "old_tool",
                "description": "Ancien",
                "deprecated": True,
                "deprecationMessage": "Migration requise",
                "sunsetAt": "2027-06-01",
            }
        },
        generated_at=_FIXED_TS,
    )
    rendered = manifest_to_markdown(manifest)
    assert "| Dépréciés | **1** |" in rendered
    assert "Deprecated" in rendered and "Sunset" in rendered
    assert "🚫" in rendered
    assert "2027-06-01" in rendered

