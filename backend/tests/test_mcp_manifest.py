# project/tests/test_mcp_manifest.py
"""Tests du générateur de manifeste MCP (S2 — Tâche 4, docs/mcp/IMPLEMENTATION_PLAN.md).

Couverture :
    1. ``safety_to_annotations``   : mapping ``thinktuning.tool/v1`` → annotations MCP ;
    2. ``resolve_posture``         : ordre déclaré > dérivé (classify_tool) > fail-closed,
       exceptions NETWORK (http_post / call_api) ;
    3. ``compile_tool``            : REUSE de ``to_json_schema`` (inputSchema), warnings ;
    4. ``build_manifest``          : tri stable, compteurs, mode strict, déterminisme ;
    5. contrat du catalogue RÉEL   : les 63 tools de ``tools_config.json``, postures
       attendues (lecture vs mutation), anti-divergence avec ``TOOL_META`` legacy ;
    6. ``entry_to_mcp_tool``       : projection vers l'entité domaine ``MCPTool`` ;
    7. rendu Markdown + I/O        : catalogue déterministe, fallbacks tolérants/stricts ;
    8. anti-divergence documentaire: ``docs/mcp/MANIFEST.md`` régénéré et commité.

Le socle MCP est self-contained (AUCUNE dépendance au SDK ``mcp``) : ces tests
n'importent rien de lourd (ni torch, ni transformers).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from app.domain.entities.mcp import MCPScopeRole, MCPVersion
from app.domain.ports.mcp_ports import MCPToolRegistryPort
from app.infrastructure.mcp.manifest_generator import (
    MUTATING_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
    ManifestError,
    build_manifest,
    compile_tool,
    entry_to_mcp_tool,
    generate_manifest,
    load_tools_config,
    manifest_to_markdown,
    resolve_posture,
    safety_to_annotations,
    write_markdown_catalog,
)
from app.infrastructure.mcp.mcp_server import InMemoryToolProvider
from ia.tools.tool_schema import to_json_schema

_LOADER_LOGGER = "thinktuning.mcp.manifest"

_FIXED_TS = "2026-09-08T00:00:00.000Z"


def _mini_manifest() -> dict[str, dict]:
    """Manifeste legacy minimal (meta format tools_config.json)."""
    return {
        "read_file": {
            "name": "read_file",
            "description": "Lit un fichier DANS la sandbox",
            "required_args": ["filename"],
            "parameters": {"filename": {"required": True, "type": "string"}},
        },
        "write_file": {
            "name": "write_file",
            "description": "Écrit `content` DANS la sandbox",
            "required_args": ["filename", "content"],
            "parameters": {
                "filename": {"required": True, "type": "string"},
                "content": {"required": True, "type": "string"},
            },
        },
    }


# --- 1. safety_to_annotations : mapping du standard v1 -------------------------


def test_safe_level_maps_to_read_only_annotations() -> None:
    safety = {"level": "safe", "requires_approval": False}
    assert safety_to_annotations(safety) == READ_ONLY_ANNOTATIONS


@pytest.mark.parametrize("level", ["restricted", "dangerous"])
def test_mutating_levels_map_to_conservative_annotations(level: str) -> None:
    safety = {"level": level, "requires_approval": True}
    assert safety_to_annotations(safety) == MUTATING_ANNOTATIONS


@pytest.mark.parametrize("safety", [None, {}, {"level": "inconnu"}, "safe", 42])
def test_absent_or_invalid_safety_fails_closed(safety: object) -> None:
    """Absent/illisible → posture mutation (jamais résolu en faveur de la lecture)."""
    assert safety_to_annotations(safety) == MUTATING_ANNOTATIONS  # type: ignore[arg-type]


def test_mapping_returns_new_dicts_not_constants() -> None:
    """Les annotations retournées sont des copies : muter le résultat ne corrompt pas le module."""
    first = safety_to_annotations({"level": "safe"})
    first["readOnlyHint"] = False
    assert safety_to_annotations({"level": "safe"})["readOnlyHint"] is True


def test_annotation_keys_match_mcp_tools_list_projection() -> None:
    """Exactement les trois hints MCP, tous booléens (contrat ``MCPTool.annotations``)."""
    for annotation_set in (READ_ONLY_ANNOTATIONS, MUTATING_ANNOTATIONS):
        assert set(annotation_set) == {"readOnlyHint", "destructiveHint", "idempotentHint"}
        assert all(isinstance(value, bool) for value in annotation_set.values())


# --- 2. resolve_posture : ordre déclaré > dérivé > défaut ----------------------


def test_declared_safety_wins_over_legacy_classification() -> None:
    """``read_file`` est READ côté legacy, mais une déclaration ``restricted`` prime."""
    definition = {"name": "read_file", "safety": {"level": "restricted", "requires_approval": True}}
    posture = resolve_posture("read_file", definition)
    assert posture.annotations == MUTATING_ANNOTATIONS
    assert posture.required_scope is MCPScopeRole.CONTRIBUTOR
    assert posture.source == "declared"


def test_declared_safe_maps_to_read_only_scope() -> None:
    definition = {"name": "whatever", "safety": {"level": "safe", "requires_approval": False}}
    posture = resolve_posture("whatever", definition)
    assert posture.annotations == READ_ONLY_ANNOTATIONS
    assert posture.required_scope is MCPScopeRole.READ_ONLY
    assert posture.source == "declared"


def test_declared_dangerous_maps_to_admin_scope() -> None:
    definition = {"name": "whatever", "safety": {"level": "dangerous", "requires_approval": True}}
    posture = resolve_posture("whatever", definition)
    assert posture.required_scope is MCPScopeRole.ADMIN


@pytest.mark.parametrize(
    ("name", "expected_read_only", "expected_scope"),
    [
        ("read_file", True, MCPScopeRole.READ_ONLY),
        ("list_dir", True, MCPScopeRole.READ_ONLY),
        ("web_search", True, MCPScopeRole.READ_ONLY),
        ("http_get", True, MCPScopeRole.READ_ONLY),
        ("env_info", True, MCPScopeRole.READ_ONLY),
        ("write_file", False, MCPScopeRole.CONTRIBUTOR),
        ("remove_path", False, MCPScopeRole.CONTRIBUTOR),
        ("run_command", False, MCPScopeRole.OPERATOR),
        ("run_python", False, MCPScopeRole.OPERATOR),
    ],
)
def test_derived_posture_from_legacy_static_classification(
    name: str, expected_read_only: bool, expected_scope: MCPScopeRole
) -> None:
    """Sans déclaration : classification statique legacy (sandbox_policy.classify_tool)."""
    posture = resolve_posture(name, {"name": name})
    assert posture.annotations["readOnlyHint"] is expected_read_only
    assert posture.required_scope is expected_scope
    assert posture.source == "derived"


@pytest.mark.parametrize("name", ["http_post", "call_api"])
def test_network_mutating_tools_are_not_read_only(name: str) -> None:
    """POST mute le serveur distant : exception NETWORK (sandbox_policy.decide + standard §1)."""
    posture = resolve_posture(name, {"name": name})
    assert posture.annotations == MUTATING_ANNOTATIONS
    assert posture.required_scope is MCPScopeRole.CONTRIBUTOR
    assert posture.source == "derived"


def test_unknown_tool_fails_closed() -> None:
    """Tool hors classifieur et sans déclaration → mutation + admin (fail-closed)."""
    unknown = "outil_inconnu_du_classifieur"
    posture = resolve_posture(unknown, {"name": unknown})
    assert posture.annotations == MUTATING_ANNOTATIONS
    assert posture.required_scope is MCPScopeRole.ADMIN
    assert posture.source == "default"


# --- 3. compile_tool : REUSE to_json_schema + warnings --------------------------


def test_input_schema_reuses_to_json_schema_output() -> None:
    """``inputSchema`` = bloc ``parameters`` de ``to_json_schema`` (reuse exact demandé)."""
    meta = _mini_manifest()["read_file"]
    entry, warnings = compile_tool("read_file", meta)
    assert warnings == []
    assert entry["inputSchema"] == to_json_schema(dict(meta))["function"]["parameters"]
    assert entry["inputSchema"]["required"] == ["filename"]
    assert entry["inputSchema"]["properties"]["filename"]["type"] == "string"


def test_compiled_entry_shape() -> None:
    entry, _ = compile_tool("read_file", _mini_manifest()["read_file"])
    assert set(entry) == {
        "name",
        "description",
        "category",
        "version",
        "inputSchema",
        "annotations",
        "safety",
        "requiredScope",
    }
    assert entry["category"] == "builtin"  # défaut standard v1 (meta legacy sans catégorie)
    assert entry["version"] == "1.0"
    assert entry["safety"] == {
        "level": "safe",
        "requires_approval": False,
        "source": "derived",
    }
    assert entry["requiredScope"] == "read_only"


# Meta legacy ``add`` : description vide (écart réel du manifeste legacy, S1).
_ADD_META = {"name": "add", "description": "", "required_args": [], "parameters": {}}


def test_empty_description_produces_warning_not_failure() -> None:
    """Meta legacy ``add`` (description vide) → warning collecté, compilation continue."""
    entry, warnings = compile_tool("add", _ADD_META)
    assert entry["name"] == "add"
    assert warnings and "add" in warnings[0] and "description" in warnings[0]


def test_compile_tool_accepts_none_meta() -> None:
    entry, warnings = compile_tool("tool_orphelin", None)
    assert entry["name"] == "tool_orphelin"
    assert warnings  # description vide → non conforme v1 → warning


# --- 4. build_manifest : document complet, tri, strict, déterminisme ------------


def test_build_manifest_sorted_and_counted() -> None:
    manifest = build_manifest(
        _mini_manifest(), version=MCPVersion.parse("0.1.0"), generated_at=_FIXED_TS
    )
    names = [entry["name"] for entry in manifest["tools"]]
    assert names == sorted(names)  # tri alphabétique : sortie stable
    assert manifest["toolCount"] == 2
    assert manifest["readOnlyCount"] == 1  # read_file seulement
    assert manifest["manifestVersion"] == "0.1.0"
    assert manifest["generatedAt"] == _FIXED_TS
    assert manifest["source"] == "ia/tools/tools_config.json"
    assert manifest["warnings"] == []


def test_build_manifest_collects_warnings_tolerant_mode() -> None:
    tools = {**_mini_manifest(), "add": _ADD_META}
    manifest = build_manifest(tools, version=MCPVersion.parse("0.1.0"), generated_at=_FIXED_TS)
    assert manifest["toolCount"] == 3
    assert any("add" in warning for warning in manifest["warnings"])


def test_build_manifest_strict_raises_on_non_conform_definition() -> None:
    tools = {**_mini_manifest(), "add": _ADD_META}
    with pytest.raises(ManifestError, match="mode strict"):
        build_manifest(tools, version=MCPVersion.parse("0.1.0"), strict=True)


def test_build_manifest_strict_ok_on_conform_input() -> None:
    manifest = build_manifest(
        _mini_manifest(), version=MCPVersion.parse("0.1.0"), strict=True
    )
    assert manifest["toolCount"] == 2


def test_build_manifest_deterministic_output() -> None:
    """Mêmes entrées → même document (horodatage fixé), octet pour octet."""
    first = build_manifest(_mini_manifest(), generated_at=_FIXED_TS)
    second = build_manifest(_mini_manifest(), generated_at=_FIXED_TS)
    assert first == second
    assert manifest_to_markdown(first) == manifest_to_markdown(second)


def test_build_manifest_defaults_version_from_pyproject() -> None:
    manifest = build_manifest(_mini_manifest(), generated_at=_FIXED_TS)
    assert manifest["manifestVersion"] == "0.1.0"


def test_build_manifest_dangerous_adds_warning() -> None:
    tools = {
        "wiper": {
            "name": "wiper",
            "description": "Tool dangereux",
            "required_args": [],
            "parameters": {},
            "safety": {"level": "dangerous", "requires_approval": True},
        }
    }
    manifest = build_manifest(tools, version=MCPVersion.parse("0.1.0"), generated_at=_FIXED_TS)
    entry = manifest["tools"][0]
    assert entry["requiredScope"] == "admin"
    assert any("dangerous" in warning for warning in manifest["warnings"])


def test_build_manifest_survives_uncompilable_entry_in_tolerant_mode() -> None:
    """Une entrée qui fait lever la compilation (``parameters`` non-objet) est isolée."""
    tools: dict = {"ok_tool": _mini_manifest()["read_file"], "cassé": {"parameters": 42}}
    manifest = build_manifest(tools, version=MCPVersion.parse("0.1.0"), generated_at=_FIXED_TS)
    assert manifest["toolCount"] == 1
    assert any("cassé" in warning and "illisible" in warning for warning in manifest["warnings"])


def test_build_manifest_strict_raises_on_uncompilable_entry() -> None:
    tools: dict = {"cassé": {"parameters": 42}}
    with pytest.raises(ManifestError, match="cassé"):
        build_manifest(tools, version=MCPVersion.parse("0.1.0"), strict=True)


def test_build_manifest_warns_on_unclassified_tools() -> None:
    """Tool absent du classifieur legacy → posture fail-closed + warning actionnable."""
    mystery_meta = {
        "name": "outil_mystere",
        "description": "Non classé",
        "required_args": [],
        "parameters": {},
    }
    manifest = build_manifest(
        {"outil_mystere": mystery_meta},
        version=MCPVersion.parse("0.1.0"),
        generated_at=_FIXED_TS,
    )
    entry = manifest["tools"][0]
    assert entry["safety"] == {
        "level": "restricted",
        "requires_approval": True,
        "source": "default",
    }
    assert entry["requiredScope"] == "admin"
    assert any(
        "outil_mystere" in warning and "safety" in warning
        for warning in manifest["warnings"]
    )


# --- 5. Contrat du catalogue RÉEL (tools_config.json, 63 tools) ----------------


@pytest.fixture(scope="module")
def real_manifest() -> dict:
    """Manifeste MCP compilé depuis le vrai ``ia/tools/tools_config.json``."""
    return generate_manifest(generated_at=_FIXED_TS)


def test_real_manifest_covers_all_declared_tools(real_manifest: dict) -> None:
    from ia.tools.tool_registry import TOOL_META  # source de vérité legacy

    names = {entry["name"] for entry in real_manifest["tools"]}
    assert names == set(TOOL_META)
    assert real_manifest["toolCount"] == len(TOOL_META) == 63


def test_real_manifest_tool_count_consistent(real_manifest: dict) -> None:
    entries = real_manifest["tools"]
    assert real_manifest["toolCount"] == len(entries)
    assert real_manifest["readOnlyCount"] == sum(
        1 for entry in entries if entry["annotations"]["readOnlyHint"]
    )


@pytest.mark.parametrize(
    "name",
    [
        # Sélection v0.1.0 (tâche 6) : les 13 tools nommés par la checklist
        # (le label « 12 » de la roadmap arrondissait la sélection). Ils
        # DOIVENT ressortir read-only du manifeste compilé : add/calc via la
        # déclaration `safety` standard v1 (tâche 6), les autres via la
        # classification READ/NETWORK de la policy legacy.
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
    ],
)
def test_real_manifest_v010_read_only_selection(real_manifest: dict, name: str) -> None:
    by_name = {entry["name"]: entry for entry in real_manifest["tools"]}
    entry = by_name[name]
    assert entry["annotations"]["readOnlyHint"] is True
    assert entry["annotations"]["destructiveHint"] is False
    assert entry["requiredScope"] == "read_only"


@pytest.mark.parametrize("name", ["add", "calc"])
def test_real_manifest_v010_declared_safe_selection_tools(
    real_manifest: dict, name: str
) -> None:
    """``add``/``calc`` : absents du classifieur legacy → posture DÉCLARÉE (tâche 6).

    Le gap documenté au livrable de la tâche 4 (fail-closed mutation + admin)
    est levé par la déclaration ``safety`` standard v1 dans tools_config.json :
    la posture read-only vient de la DÉCLARATION (source « declared »), jamais
    d'une devinette du manifeste (règle : ne jamais deviner une posture de
    lecture).
    """
    by_name = {entry["name"]: entry for entry in real_manifest["tools"]}
    entry = by_name[name]
    assert entry["safety"] == {
        "level": "safe",
        "requires_approval": False,
        "source": "declared",
    }
    assert entry["annotations"] == READ_ONLY_ANNOTATIONS
    assert entry["requiredScope"] == "read_only"


@pytest.mark.parametrize(
    "name",
    [
        "write_file",
        "write_json",
        "append_file",
        "remove_path",
        "move_path",
        "run_command",
        "run_python",
        "http_post",
        "call_api",
        "start_training",
        "cancel_training",
    ],
)
def test_real_manifest_mutating_tools_are_conservative(real_manifest: dict, name: str) -> None:
    by_name = {entry["name"]: entry for entry in real_manifest["tools"]}
    entry = by_name[name]
    assert entry["annotations"]["readOnlyHint"] is False
    assert entry["annotations"]["destructiveHint"] is True
    assert entry["requiredScope"] != "read_only"


def test_real_manifest_add_schema(real_manifest: dict) -> None:
    by_name = {entry["name"]: entry for entry in real_manifest["tools"]}
    schema = by_name["add"]["inputSchema"]
    assert schema["type"] == "object"
    assert schema["properties"]["a"]["type"] == "number"
    assert schema["properties"]["b"]["type"] == "number"
    assert schema["required"] == ["a", "b"]


def test_real_manifest_annotations_are_booleans_only(real_manifest: dict) -> None:
    for entry in real_manifest["tools"]:
        assert set(entry["annotations"]) == {"readOnlyHint", "destructiveHint", "idempotentHint"}
        assert all(isinstance(value, bool) for value in entry["annotations"].values())
        assert entry["requiredScope"] in {"read_only", "contributor", "operator", "admin"}


def test_real_manifest_flags_legacy_gap_known_from_s1(real_manifest: dict) -> None:
    """`add` (description vide dans tools_config.json) → warning tracé, pas masqué."""
    assert any("add" in warning for warning in real_manifest["warnings"])


# --- 6. entry_to_mcp_tool : couture vers l'entité domaine (tâche 6) --------------


def test_entry_to_mcp_tool_satisfies_registry_port(real_manifest: dict) -> None:
    entry = real_manifest["tools"][0]
    provider = InMemoryToolProvider([entry_to_mcp_tool(entry, lambda _args: "ok")])
    assert isinstance(provider, MCPToolRegistryPort)  # le port est satisfait end-to-end


def test_entry_to_mcp_tool_projection(real_manifest: dict) -> None:
    entry = next(e for e in real_manifest["tools"] if e["name"] == "read_file")
    tool = entry_to_mcp_tool(entry, lambda _args: "contenu")
    assert tool.name == "read_file"
    assert tool.input_schema == entry["inputSchema"]
    assert tool.annotations == entry["annotations"]
    assert tool.required_scope is MCPScopeRole.READ_ONLY
    assert tool.handler({}) == "contenu"
    assert tool.to_dict()["name"] == "read_file"  # projection tools/list


def test_entry_to_mcp_tool_explicit_scope_override(real_manifest: dict) -> None:
    entry = real_manifest["tools"][0]
    tool = entry_to_mcp_tool(entry, lambda _args: "ok", required_scope=MCPScopeRole.ADMIN)
    assert tool.required_scope is MCPScopeRole.ADMIN


def test_entry_to_mcp_tool_invalid_scope_fails_fast(real_manifest: dict) -> None:
    entry = dict(real_manifest["tools"][0])
    entry["requiredScope"] = "superuser"
    with pytest.raises(ValueError):
        entry_to_mcp_tool(entry, lambda _args: "ok")


def test_manifest_entries_feed_in_memory_provider(real_manifest: dict) -> None:
    """Le manifeste alimente directement un registre MCP (wiring tâche 6)."""
    entries = [e for e in real_manifest["tools"] if e["annotations"]["readOnlyHint"]][:12]
    provider = InMemoryToolProvider(
        [entry_to_mcp_tool(entry, lambda _args: "ok") for entry in entries]
    )
    assert {tool.name for tool in provider.list_tools()} == {entry["name"] for entry in entries}


# --- 7. Rendu Markdown (catalogue produit) -------------------------------------


def test_markdown_header_contains_product_metadata(real_manifest: dict) -> None:
    doc = manifest_to_markdown(real_manifest)
    assert doc.startswith("# Manifeste MCP ThinkTuning — v0.1.0")
    assert "FICHIER GÉNÉRÉ" in doc
    assert f"**{real_manifest['toolCount']}**" in doc
    assert f"**{real_manifest['readOnlyCount']}**" in doc
    assert real_manifest["generatedAt"] in doc


def test_markdown_lists_every_tool_and_schema(real_manifest: dict) -> None:
    doc = manifest_to_markdown(real_manifest)
    for entry in real_manifest["tools"]:
        assert f"`{entry['name']}`" in doc
        assert f"### `{entry['name']}`" in doc
    assert doc.count("```json") == real_manifest["toolCount"]


def test_markdown_renders_warnings_section(real_manifest: dict) -> None:
    doc = manifest_to_markdown(real_manifest)
    if real_manifest["warnings"]:
        assert "## Avertissements de compilation" in doc


def test_markdown_escapes_pipes_and_truncates_long_descriptions() -> None:
    manifest = build_manifest(
        {
            "tricky": {
                "name": "tricky",
                "description": "a | b | " + "x" * 300,
                "required_args": [],
                "parameters": {},
            }
        },
        version=MCPVersion.parse("0.1.0"),
        generated_at=_FIXED_TS,
    )
    row = next(
        line
        for line in manifest_to_markdown(manifest).splitlines()
        if line.startswith("| `tricky`")
    )
    assert "\\|" in row  # pipes échappés : tableau intègre
    assert len(row) < 400  # description tronquée


# --- 8. I/O : chargement tolérant / strict --------------------------------------


def test_load_default_discovery_reads_legacy_manifest() -> None:
    tools = load_tools_config()
    assert "read_file" in tools
    assert "web_search" in tools
    assert len(tools) == 63


def test_load_explicit_str_path(tmp_path: Path) -> None:
    path = tmp_path / "tools_config.json"
    path.write_text(json.dumps({"tools": {"now": {"name": "now"}}}), encoding="utf-8")
    assert load_tools_config(str(path)) == {"now": {"name": "now"}}


def test_load_missing_file_falls_back_with_warning(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOADER_LOGGER):
        tools = load_tools_config(tmp_path / "absent.json")
    assert tools == {}
    assert any("introuvable" in record.getMessage() for record in caplog.records)


def test_load_missing_file_strict_raises(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="introuvable"):
        load_tools_config(tmp_path / "absent.json", strict=True)


def test_load_invalid_json_falls_back(tmp_path: Path, caplog) -> None:
    path = tmp_path / "tools_config.json"
    path.write_text("not-json{", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=_LOADER_LOGGER):
        assert load_tools_config(path) == {}
    assert any("illisible" in record.getMessage() for record in caplog.records)


def test_load_invalid_json_strict_raises(tmp_path: Path) -> None:
    path = tmp_path / "tools_config.json"
    path.write_text("not-json{", encoding="utf-8")
    with pytest.raises(ManifestError, match="illisible"):
        load_tools_config(path, strict=True)


def test_load_unexpected_structure_falls_back(tmp_path: Path) -> None:
    path = tmp_path / "tools_config.json"
    path.write_text(json.dumps({"no_tools_key": []}), encoding="utf-8")
    assert load_tools_config(path) == {}
    with pytest.raises(ManifestError, match="structure inattendue"):
        load_tools_config(path, strict=True)


def test_generate_manifest_end_to_end(tmp_path: Path) -> None:
    path = tmp_path / "tools_config.json"
    path.write_text(json.dumps({"tools": _mini_manifest()}), encoding="utf-8")
    manifest = generate_manifest(path, generated_at=_FIXED_TS)
    assert manifest["toolCount"] == 2
    assert manifest["readOnlyCount"] == 1


def test_write_markdown_catalog(tmp_path: Path, real_manifest: dict) -> None:
    output = tmp_path / "nested" / "MANIFEST.md"
    written = write_markdown_catalog(output, manifest=real_manifest)
    assert written == output
    content = output.read_text(encoding="utf-8")
    assert content == manifest_to_markdown(real_manifest)


# --- 9. Anti-divergence : docs/mcp/MANIFEST.md régénéré et commité --------------


def test_committed_catalog_is_in_sync(real_manifest: dict) -> None:
    """Checklist CI : « MANIFEST.md régénéré et commité ».

    Le catalogue commité doit refléter le manifeste compilé ACTUEL (version +
    compteurs). Si tools_config.json change, régénérer :
        python -m app.infrastructure.mcp.manifest_generator  (depuis backend/)
    """
    from app.infrastructure.mcp.manifest_generator import _default_catalog_path

    catalog = _default_catalog_path()
    assert catalog.is_file(), f"{catalog} absent — régénérer le catalogue (tâche 4)"
    content = catalog.read_text(encoding="utf-8")
    assert f"# Manifeste MCP ThinkTuning — v{real_manifest['manifestVersion']}" in content
    assert f"**{real_manifest['toolCount']}**" in content
    assert f"**{real_manifest['readOnlyCount']}**" in content



