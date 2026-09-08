# project/tests/test_legacy_tool_provider.py
"""Tests du provider MCP read-only — projection du registre legacy (S2, tâche 6 ; extension S3, tâche 7).

Couverture du livrable « 12 tools read-only » (13 noms réels, la checklist de
l'IMPLEMENTATION_PLAN.md arrondissait le compte) puis de l'extension v1.0.0
(``V100_READ_ONLY_TOOLS`` — 25 tools, tâche 7) :

    1. sélection : ``V010_READ_ONLY_TOOLS`` = exactement les 13 tools nommés,
       exposés en ordre alphabétique (déterministe), aucun tool mutatif ;
       ``V100_READ_ONLY_TOOLS`` = les 25 tools (13 v0.1.0 + 12 extension) ;
    2. contrat port : ``MCPToolRegistryPort`` (tâche 3) — ``list_tools`` /
       ``call_tool`` + erreurs ``ToolError`` (→ MCP ``isError``) ;
    3. annotations : ``readOnlyHint: true`` / ``destructiveHint: false`` /
       ``idempotentHint: true`` + ``required_scope == READ_ONLY`` ;
    4. anti-divergence : bit-à-bit aligné sur le manifeste compilé
       (description, ``inputSchema``, annotations, scope) ;
    5. exécution réelle par délégation legacy : calculs purs (``add``,
       ``calc``) ; fichiers DANS une sandbox temporaire isolée
       (``AGENT_SANDBOX_ROOT`` → tmp_path) : ``read_file``, ``count_lines``,
       ``head_file``, ``file_info``, ``file_checksum``, ``list_dir``,
       ``find_file`` ; tools réseau : validation synchrone (args requis)
       SANS appel réseau (aucune I/O externe dans cette suite) ;
    6. erreurs métier : tool inconnu, arguments requis manquants, exceptions
       legacy propagées proprement (``FileNotFoundError`` → ``ToolError``),
       ``ToolError`` legacy translucide (jamais re-wrappée) ;
    7. fail-closed à la construction : posture mutation / nom inconnu /
       implémentation absente → EXCLUS du catalogue (warning tracé) ;
    8. intégration serveur : ``build_mcp_server()`` par défaut = bootstrap S1
       (2 tools) + sélection v1.0.0 (25) = 27 tools, appels ``tools/call``
       JSON-RPC de bout en bout (``add``, ``read_file``).

Aucun appel réseau ni dépendance lourde (ni torch, ni transformers).
"""

from __future__ import annotations

import json

import pytest

from app.domain.entities.mcp import MCPScopeRole
from app.domain.ports.mcp_ports import MCPToolRegistryPort
from app.infrastructure.mcp.manifest_generator import generate_manifest
from app.infrastructure.mcp.mcp_server import ToolError
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.legacy_tool_provider import (
    V010_READ_ONLY_TOOLS,
    V100_READ_ONLY_TOOLS,
    LegacyRegistryToolProvider,
    build_v010_read_only_provider,
    build_v100_read_only_provider,
)

_LOGGER = "thinktuning.mcp.tools"
_FIXED_TS = "2026-09-08T00:00:00.000Z"

# Checklist exacte de la tâche 6 (docs/mcp/IMPLEMENTATION_PLAN.md).
_CHECKLIST_V010 = {
    "add",
    "calc",
    "web_search",
    "web_fetch",
    "web_read",
    "http_get",
    "read_file",
    "list_dir",
    "find_file",
    "file_info",
    "file_checksum",
    "head_file",
    "count_lines",
}

# Checklist v1.0.0 (tâche 7) — V010 (13) + extension read-only (12) =
# « 25 tools » (docs/mcp/MCP_ROADMAP.md, v1.0.0 Public Beta).
_CHECKLIST_V100 = _CHECKLIST_V010 | {
    "job_list",
    "job_get",
    "model_versions",
    "dataset_stats",
    "predict_sentiment",
    "env_info",
    "disk_usage",
    "gpu_info",
    "now",
    "tail_file",
    "read_json",
    "search_in_files",
}

# Représentants de MAUVAISES postures — jamais exposés par la surface read-only.
_MUTATING_LEGACY_TOOLS = {
    "write_file",
    "write_json",
    "run_command",
    "http_post",
    "remove_path",
}


@pytest.fixture
def provider() -> LegacyRegistryToolProvider:
    """Provider réel de la surface v0.1.0 explicite (13 tools — comme en prod v0.1.0)."""
    return build_v010_read_only_provider()


@pytest.fixture
def provider_v100() -> LegacyRegistryToolProvider:
    """Provider réel de la surface v1.0.0 (25 tools — par défaut, comme en prod)."""
    return build_v100_read_only_provider()


@pytest.fixture(scope="module")
def manifest() -> dict:
    """Manifeste compilé depuis le vrai ``ia/tools/tools_config.json``."""
    return generate_manifest(generated_at=_FIXED_TS)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """Sandbox temporaire : AGENT_SANDBOX_ROOT → tmp_path + fichiers de test."""
    root = tmp_path / "sandbox"
    root.mkdir()
    (root / "nested").mkdir()
    (root / "a.txt").write_text(
        "première ligne\nseconde ligne\ntroisième ligne\n", encoding="utf-8"
    )
    (root / "nested" / "b.txt").write_text("caché\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(root))
    return root


# --- 1. Sélection v0.1.0 ----------------------------------------------------------


def test_v010_selection_matches_task6_checklist() -> None:
    """``V010_READ_ONLY_TOOLS`` = exactement la checklist (13 tools nommés)."""
    assert set(V010_READ_ONLY_TOOLS) == _CHECKLIST_V010


def test_v100_selection_matches_task7_checklist() -> None:
    """``V100_READ_ONLY_TOOLS`` = V010 ∪ extension read-only = les 25 tools."""
    assert set(V100_READ_ONLY_TOOLS) == _CHECKLIST_V100
    assert len(V100_READ_ONLY_TOOLS) == 25


def test_v100_provider_exposes_25_tools_sorted(provider_v100) -> None:
    """``build_v100_read_only_provider()`` expose les 25 tools, sans doublon."""
    names = [tool.name for tool in provider_v100.list_tools()]
    assert set(names) == _CHECKLIST_V100
    assert names == sorted(names)
    assert len(names) == 25


@pytest.mark.parametrize(
    "name",
    ["job_list", "job_get", "model_versions", "dataset_stats", "predict_sentiment"],
)
def test_v100_metier_tools_are_declared_safe(name: str) -> None:
    """Les 5 tools métier : posture read-only via la DÉCLARATION ``safety`` (tâche 7).

    Auparavant UNKNOWN → fail-closed (mutation + admin) ; la déclaration
    ``safety: safe`` (tools_config.json) lève le gap sans toucher au classifieur.
    """
    from app.infrastructure.mcp.manifest_generator import compile_tool
    from ia.tools.tool_registry import TOOL_META

    entry, _ = compile_tool(name, TOOL_META[name])
    assert entry["safety"] == {
        "level": "safe",
        "requires_approval": False,
        "source": "declared",
    }
    assert entry["annotations"] == {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    }
    assert entry["requiredScope"] == "read_only"


def test_v100_every_tool_is_read_only_and_read_only_scope(provider_v100) -> None:
    expected = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    }
    for tool in provider_v100.list_tools():
        assert tool.annotations == expected, tool.name
        assert tool.required_scope is MCPScopeRole.READ_ONLY, tool.name


def test_provider_exposes_exactly_the_selection_sorted(provider) -> None:
    """``list_tools()`` = les 13 tools, ordre alphabétique, sans doublon."""
    names = [tool.name for tool in provider.list_tools()]
    assert set(names) == _CHECKLIST_V010
    assert names == sorted(names)
    assert len(names) == 13


def test_no_mutating_legacy_tool_leaks_in_selection(provider) -> None:
    """Aucun tool de posture mutation (write/exec/réseau POST) exposé."""
    listed = {tool.name for tool in provider.list_tools()}
    assert listed.isdisjoint(_MUTATING_LEGACY_TOOLS)


# --- 2. Contrat du port -----------------------------------------------------------


def test_provider_implements_mcp_tool_registry_port(provider) -> None:
    assert isinstance(provider, MCPToolRegistryPort)


def test_call_tool_unknown_raises_tool_error(provider) -> None:
    with pytest.raises(ToolError, match="Unknown tool"):
        provider.call_tool("outil_inexistant", {})


def test_call_tool_unknown_even_with_args(provider) -> None:
    """L'inconnu est indiscernable d'un tool absent — aucune métadonnée inventée."""
    with pytest.raises(ToolError, match="Unknown tool"):
        provider.call_tool("predict_sentiment", {"text": "boom"})


def test_required_args_are_enforced(provider) -> None:
    """Argument requis manquant → ToolError explicite (jamais TypeError)."""
    with pytest.raises(ToolError, match="add"):
        provider.call_tool("add", {"a": 2.0})
    with pytest.raises(ToolError, match="read_file"):
        provider.call_tool("read_file", {"max_bytes": 10})


# --- 3. Annotations & scope -------------------------------------------------------


def test_every_tool_is_read_only_and_idempotent(provider) -> None:
    expected = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
    }
    for tool in provider.list_tools():
        assert tool.annotations == expected, tool.name


def test_every_tool_requires_read_only_scope(provider) -> None:
    for tool in provider.list_tools():
        assert tool.required_scope is MCPScopeRole.READ_ONLY, tool.name


def test_every_tool_has_populated_input_schema(provider) -> None:
    for tool in provider.list_tools():
        assert tool.input_schema.get("type") == "object", tool.name
        assert isinstance(tool.input_schema.get("properties", {}), dict), tool.name


@pytest.mark.parametrize(
    ("name", "required"),
    [
        ("add", {"a", "b"}),
        ("calc", {"expression"}),
        ("read_file", {"path"}),
        ("web_search", {"query"}),
        ("http_get", {"url"}),
        ("file_checksum", {"path"}),
    ],
)
def test_input_schema_declares_required_arguments(
    provider, name: str, required: set[str]
) -> None:
    by_name = {tool.name: tool for tool in provider.list_tools()}
    properties = by_name[name].input_schema["properties"]
    declared_required = set(by_name[name].input_schema.get("required", []))
    assert required <= set(properties)
    assert required <= declared_required


# --- 4. Anti-divergence manifeste / runtime ---------------------------------------


def test_runtime_matches_compiled_manifest(provider, manifest) -> None:
    """Le catalogue runtime est bit-à-bit celui du manifeste compilé (REUSE)."""
    by_name = {entry["name"]: entry for entry in manifest["tools"]}
    for tool in provider.list_tools():
        entry = by_name[tool.name]
        assert tool.description == entry["description"], tool.name
        assert tool.input_schema == entry["inputSchema"], tool.name
        assert tool.annotations == entry["annotations"], tool.name
        assert tool.required_scope.value == entry["requiredScope"], tool.name
        assert entry["annotations"]["readOnlyHint"] is True, tool.name


def test_v010_selection_is_read_only_in_compiled_manifest(manifest) -> None:
    """Toute la sélection v0.1.0 ressort read-only du manifeste réel."""
    by_name = {entry["name"]: entry for entry in manifest["tools"]}
    for name in _CHECKLIST_V010:
        entry = by_name[name]
        assert entry["annotations"]["readOnlyHint"] is True, name
        assert entry["requiredScope"] == "read_only", name


# --- 5. Exécution réelle (délégation legacy) --------------------------------------


def test_add_executes_legacy(provider) -> None:
    text = provider.call_tool("add", {"a": 2.0, "b": 3.0})
    assert text in ("5", "5.0")


def test_calc_executes_legacy(provider) -> None:
    text = provider.call_tool("calc", {"expression": "2 + 3"})
    payload = json.loads(text)
    assert payload["expression"] == "2 + 3"
    assert payload["result"] in (5, 5.0)


@pytest.mark.parametrize(
    ("name", "arguments", "needle"),
    [
        ("read_file", {"path": "a.txt"}, "seconde ligne"),
        ("count_lines", {"path": "a.txt"}, '"lines": 3'),
        ("head_file", {"path": "a.txt", "max_lines": 1}, '"returned_lines": 1'),
        ("file_info", {"path": "a.txt"}, '"type": "file"'),
        ("file_checksum", {"path": "a.txt", "algo": "sha256"}, '"algorithm": "sha256"'),
        ("list_dir", {"path": "."}, '"a.txt"'),
        ("find_file", {"pattern": r"\.txt$"}, '"a.txt"'),
    ],
)
def test_file_tools_execute_in_sandbox(provider, sandbox, name, arguments, needle) -> None:
    """Chaque tool fichier s'exécute DANS la sandbox isolée (safe_resolve)."""
    text = provider.call_tool(name, arguments)
    assert needle in text, text


def test_file_tools_cannot_escape_sandbox(provider, sandbox) -> None:
    """``safe_resolve`` : une évasion hors racine sandbox est refusée (ToolError)."""
    with pytest.raises(ToolError):
        provider.call_tool("read_file", {"path": "../README.md"})


def test_network_tools_enforce_required_args_without_network(provider) -> None:
    """Tools réseau : validation synchrone des args — AUCUN appel réseau ici."""
    for name in ("web_search", "web_fetch", "web_read", "http_get"):
        with pytest.raises(ToolError):
            provider.call_tool(name, {})


# --- 6. Erreurs métier ------------------------------------------------------------


def test_legacy_exception_becomes_tool_error(provider, sandbox) -> None:
    """Exception legacy (FileNotFoundError) → ToolError, jamais un crash."""
    with pytest.raises(ToolError, match="FileNotFoundError"):
        provider.call_tool("read_file", {"path": "introuvable.txt"})


def test_legacy_tool_error_is_propagated_transparently() -> None:
    """Un ``ToolError`` des implémentations legacy remonte TEL QUEL (translucide)."""
    error = ToolError("boom métier legacy")

    def boom():
        raise error

    custom = LegacyRegistryToolProvider(
        selection={"boom_tool"},
        tools={"boom_tool": boom},
        manifest={
            "boom_tool": {
                "name": "boom_tool",
                "description": "Tool qui lève ToolError",
                "parameters": {},
                "safety": {"level": "safe", "requires_approval": False},
            }
        },
    )
    with pytest.raises(ToolError) as excinfo:
        custom.call_tool("boom_tool", {})
    assert excinfo.value is error


# --- 7. Fail-closed à la construction ---------------------------------------------


def test_mutation_posture_selection_is_excluded() -> None:
    """``write_file`` (posture mutation) ne peut JAMAIS être exposé par la surface RO."""
    custom = LegacyRegistryToolProvider(selection={"read_file", "write_file"})
    names = {tool.name for tool in custom.list_tools()}
    assert names == {"read_file"}


def test_unknown_selection_entries_are_excluded() -> None:
    """Nom inconnu → exclu (aucune métadonnée synthétisée à partir du nom)."""
    custom = LegacyRegistryToolProvider(selection={"tout_a_fait_inconnu"})
    assert custom.list_tools() == []


def test_missing_legacy_implementation_is_excluded() -> None:
    """Nom connu mais implémentation absente → exclu (jamais de stub muet)."""
    custom = LegacyRegistryToolProvider(selection={"read_file"}, tools={})
    assert custom.list_tools() == []


def test_exclusions_are_logged(caplog) -> None:
    """Chaque exclusion est tracée (warning actionnable → y rattraper)."""
    with caplog.at_level("WARNING", logger=_LOGGER):
        LegacyRegistryToolProvider(
            selection={"write_file", "tout_a_fait_inconnu", "read_file"},
            tools={"read_file": lambda path: path},
        )
    messages = " ".join(record.message for record in caplog.records)
    assert "write_file" in messages
    assert "tout_a_fait_inconnu" in messages


# --- 8. Intégration serveur -------------------------------------------------------


def test_default_server_exposes_bootstrap_plus_v100() -> None:
    """Surface par défaut = bootstrap S1 (2) + sélection v1.0.0 (25) = 27 tools."""
    server = build_mcp_server()
    reply = json.loads(
        server.handle_text(
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        )
    )
    names = {tool["name"] for tool in reply["result"]["tools"]}
    assert {"mcp_version", "server_info"} <= names
    assert _CHECKLIST_V100 <= names
    assert len(names) == 27


def test_default_server_read_only_posture() -> None:
    server = build_mcp_server()
    reply = json.loads(
        server.handle_text(
            json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        )
    )
    for tool in reply["result"]["tools"]:
        if tool["name"] in _CHECKLIST_V100:
            assert tool["annotations"]["readOnlyHint"] is True, tool["name"]


def test_call_add_end_to_end() -> None:
    server = build_mcp_server()
    reply = json.loads(
        server.handle_text(
            json.dumps({
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "add", "arguments": {"a": 2.0, "b": 3.0}},
            })
        )
    )
    assert reply["result"]["isError"] is False
    assert reply["result"]["content"][0]["text"] in ("5", "5.0")


def test_call_file_tool_end_to_end(sandbox) -> None:
    server = build_mcp_server()
    reply = json.loads(
        server.handle_text(
            json.dumps({
                "jsonrpc": "2.0", "id": 4, "method": "tools/call",
                "params": {"name": "count_lines", "arguments": {"path": "a.txt"}},
            })
        )
    )
    assert reply["result"]["isError"] is False
    assert '"lines": 3' in reply["result"]["content"][0]["text"]


def test_read_only_scope_sees_whole_v100_surface() -> None:
    """Scope read_only : les  25 tools (exigence minimale) sont visibles."""
    server = build_mcp_server(scope=MCPScopeRole.READ_ONLY)
    reply = json.loads(
        server.handle_text(
            json.dumps({"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
        )
    )
    names = {tool["name"] for tool in reply["result"]["tools"]}
    assert _CHECKLIST_V100 <= names

