# project/app/infrastructure/mcp/manifest_generator.py
"""Générateur de manifeste MCP — compile ``tools_config.json`` → manifeste MCP (S2, tâche 4).

Rec. 3 du mapping (docs/mcp/MCP_IMPLEMENTATION_MAPPING.md) : le « protocol
interne » existant (``ia/tools/tools_config.json`` + ``ia/tools/tool_schema.py``)
est la SOURCE DE CONFIGURATION — ce module le COMPILE en manifeste MCP sans rien
réinventer :

    tools_config.json (entrées legacy TOOL_META)
        → ``from_meta_format()``    normalisation standard ``thinktuning.tool/v1``
        → ``to_json_schema()``      JSON Schema → ``inputSchema`` MCP (REUSE exact)
        → ``safety`` → ``annotations`` (readOnlyHint / destructiveHint / idempotentHint)

Résolution de la posture de sûreté d'un tool (ordre déterministe, documenté) :

    1. DÉCLARÉ    — ``safety`` présente dans la définition (standard v1) :
                    la déclaration design-time prime toujours ;
    2. DÉRIVÉ     — classification statique legacy ``classify_tool()``
                    (app/agent/policies/sandbox_policy.py — Rec. 7/11 du mapping :
                    la policy existante est PROJETÉE en annotations, pas dupliquée) ;
    3. DÉFAUT     — fail-closed : posture « mutation » (le doute n'est jamais
                    résolu en faveur de la lecture).

Exceptions NETWORK : ``http_post`` et ``call_api`` mutent le serveur DISTANT
(règle dure de ``sandbox_policy.decide`` pour le POST ; ``call_api`` est
« restricted » dans docs/TOOL_STANDARD.md §1) → posture mutation malgré leur
catégorie NETWORK.

SÉPARATION DES RESPONSABILITÉS (tâche 4 ≠ tâche 5) :
    - ce module (tâche 4)  : manifeste DESIGN-TIME, statique, sans arguments —
      un catalogue déclaratif fidèle aux sources existantes ;
    - policy_adapter (tâche 5) : décision RUNTIME par appel (``decide_action``,
      chemins sensibles, anti-SSRF, SQL mutant) → annotations + filtre de scope.

Le manifeste produit est DÉCLARATIF (aucun handler) : l'exécution est câblée à
la tâche 6 (12 tools read-only) via ``entry_to_mcp_tool``. Toute I/O fichier est
confinée dans ``load_tools_config`` / ``write_markdown_catalog`` (règle
hexagonale), la compilation restant pure et déterministe.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from app.agent.policies.sandbox_policy import classify_tool
from app.domain.entities.mcp import MCPScopeRole, MCPTool, MCPVersion
from app.domain.entities.plan import ActionCategory, utc_now_iso
from app.infrastructure.mcp.protocol import (
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    empty_input_schema,
)
from app.infrastructure.mcp.version_loader import load_mcp_version
from ia.tools.tool_schema import (
    DEFAULT_CATEGORY,
    DEFAULT_VERSION,
    SAFETY_LEVELS,
    from_meta_format,
    to_json_schema,
    validate_tool_definition,
)

logger = logging.getLogger("thinktuning.mcp.manifest")

_MANIFEST_FILENAME = "tools_config.json"
_DEFAULT_SOURCE_LABEL = "ia/tools/tools_config.json"
_CATALOG_RELPATH = Path("docs") / "mcp" / "MANIFEST.md"

# Annotations MCP (spec « annotations » de tools/list) — les deux postures.
READ_ONLY_ANNOTATIONS: dict[str, bool] = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
}
MUTATING_ANNOTATIONS: dict[str, bool] = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
}

# Provenance de la posture (traçabilité du catalogue).
SAFETY_SOURCE_DECLARED = "declared"
SAFETY_SOURCE_DERIVED = "derived"
SAFETY_SOURCE_DEFAULT = "default"

# Tools NETWORK capables de muter le serveur distant (voir docstring module).
_NETWORK_MUTATING_TOOLS = frozenset({"http_post", "call_api"})

# Description tronquée dans le tableau du catalogue Markdown.
_MD_DESCRIPTION_MAX_CHARS = 110


class ManifestError(ValueError):
    """Manifeste non compilable (mode strict : définition invalide, JSON illisible)."""


class ToolPosture(NamedTuple):
    """Posture de sûreté résolue d'un tool (annotations MCP + scope + provenance)."""

    annotations: dict[str, bool]
    required_scope: MCPScopeRole
    source: str


# ---------------------------------------------------------------------------
# Compilation pure (aucune I/O)
# ---------------------------------------------------------------------------


def safety_to_annotations(safety: Mapping[str, Any] | None) -> dict[str, bool]:
    """Mappe ``thinktuning.tool/v1 safety`` → annotations MCP (déterministe).

    Règles (docs/TOOL_STANDARD.md §1-2) :
        - ``safe``       : lecture pure → readOnly, non destructif, idempotent ;
        - ``restricted`` : peut muter (écriture, POST…) → validation humaine,
          posture mutation (readOnlyHint false, destructiveHint true) ;
        - ``dangerous``  : bloqué par la policy — posture mutation + scope
          maximal (la visibilité admin n'est pas une permission d'exécution :
          le gate ``approvals`` reste insurmontable) ;
        - absent / illisible : fail-closed → posture mutation (même sémantique
          que ``DEFAULT_SAFETY`` du standard pour un tool non déclaré).

    Returns:
        Une NOUVELLE dict (les constantes ne sont jamais exposées mutables).
    """
    level = safety.get("level") if isinstance(safety, Mapping) else None
    if level == "safe":
        return dict(READ_ONLY_ANNOTATIONS)
    return dict(MUTATING_ANNOTATIONS)


def _posture_from_category(category: ActionCategory) -> ToolPosture:
    """Classification statique legacy → posture MCP (Rec. 7 du mapping).

    Alignée sur ``MCPScopeRole`` (docs/mcp/MCP_SECURITY.md) : read_only (lecture)
    < contributor (write filtré) < operator (exec filtré) < admin (full access).
    """
    if category in (ActionCategory.READ, ActionCategory.SYSTEM, ActionCategory.NETWORK):
        return ToolPosture(
            dict(READ_ONLY_ANNOTATIONS), MCPScopeRole.READ_ONLY, SAFETY_SOURCE_DERIVED
        )
    if category is ActionCategory.EXEC:
        return ToolPosture(
            dict(MUTATING_ANNOTATIONS), MCPScopeRole.OPERATOR, SAFETY_SOURCE_DERIVED
        )
    if category in (ActionCategory.WRITE, ActionCategory.DELETE):
        return ToolPosture(
            dict(MUTATING_ANNOTATIONS), MCPScopeRole.CONTRIBUTOR, SAFETY_SOURCE_DERIVED
        )
    # UNKNOWN : prudence legacy (défaut approvals = APPROVE) — fail-closed :
    # un tool non classé n'est visible que du rôle maximal.
    return ToolPosture(dict(MUTATING_ANNOTATIONS), MCPScopeRole.ADMIN, SAFETY_SOURCE_DEFAULT)


def resolve_posture(name: str, definition: Mapping[str, Any]) -> ToolPosture:
    """Résout la posture d'un tool : déclaré > dérivé (classification) > défaut.

    Args:
        name:        nom du tool (clé du manifeste legacy) ;
        definition:  définition normalisée standard v1 (``from_meta_format``).

    Note: l'override legacy ``approval`` (auto/manual/blocked) ne pilote PAS les
    annotations — seule la déclaration ``safety`` du standard v1 est contractuelle
    pour le manifeste ; ``approval`` reste un override runtime du gate.
    """
    safety = definition.get("safety")
    if isinstance(safety, Mapping) and safety.get("level") in SAFETY_LEVELS:
        level = str(safety["level"])
        scope = {
            "safe": MCPScopeRole.READ_ONLY,
            "restricted": MCPScopeRole.CONTRIBUTOR,
            "dangerous": MCPScopeRole.ADMIN,
        }[level]
        return ToolPosture(safety_to_annotations(safety), scope, SAFETY_SOURCE_DECLARED)

    category = classify_tool(name)
    if name in _NETWORK_MUTATING_TOOLS and category in (
        ActionCategory.NETWORK,
        ActionCategory.UNKNOWN,
    ):
        # Exception standard v1 §1 (call_api = restricted) / règle dure decide()
        # (POST = mutation serveur) — y compris quand le classifieur legacy ne
        # connaît pas le tool (UNKNOWN) : la norme documentée prime.
        return ToolPosture(
            dict(MUTATING_ANNOTATIONS), MCPScopeRole.CONTRIBUTOR, SAFETY_SOURCE_DERIVED
        )
    if category is not ActionCategory.UNKNOWN:
        return _posture_from_category(category)
    # Ni déclaration, ni classification connue : fail-closed (mutation + admin).
    return ToolPosture(safety_to_annotations(None), MCPScopeRole.ADMIN, SAFETY_SOURCE_DEFAULT)


def compile_tool(
    name: str, meta: Mapping[str, Any] | None
) -> tuple[dict[str, Any], list[str]]:
    """Compile UNE entrée legacy en entrée de manifeste MCP.

    Pipeline (aucune réinvention) :
        1. ``from_meta_format``         : meta legacy → définition standard v1 ;
        2. ``validate_tool_definition`` : validation standard (warnings si écart) ;
        3. ``resolve_posture``          : safety → annotations + scope ;
        4. ``to_json_schema``           : réutilisé tel quel — seul le bloc
           ``parameters`` est exposé comme ``inputSchema`` MCP (l'enveloppe
           ``{"type": "function", ...}`` est spécifique au function-calling LLM).

    Returns:
        ``(entrée, warnings)`` — warnings affichables, déterministes, préfixés
        par le nom du tool ; jamais levés (le mode strict est porté par
        ``build_manifest``).
    """
    definition = from_meta_format(name, dict(meta) if isinstance(meta, Mapping) else None)
    valid, issues = validate_tool_definition(definition)
    posture = resolve_posture(name, definition)
    declared_level = (
        definition.get("safety", {}).get("level", "restricted")
        if isinstance(definition.get("safety"), Mapping)
        else "restricted"
    )
    entry: dict[str, Any] = {
        "name": name,
        "description": str(definition.get("description", "") or ""),
        "category": definition.get("category", DEFAULT_CATEGORY),
        "version": definition.get("version", DEFAULT_VERSION),
        "inputSchema": to_json_schema(definition)["function"]["parameters"],
        "annotations": posture.annotations,
        "safety": {
            "level": "safe" if posture.annotations["readOnlyHint"] else declared_level,
            "requires_approval": not posture.annotations["readOnlyHint"],
            "source": posture.source,
        },
        "requiredScope": posture.required_scope.value,
    }
    warnings = [f"{name}: {issue}" for issue in issues] if not valid else []
    return entry, warnings


def build_manifest(
    tools: Mapping[str, Mapping[str, Any]],
    *,
    version: MCPVersion | None = None,
    generated_at: str | None = None,
    source: str = _DEFAULT_SOURCE_LABEL,
    strict: bool = False,
) -> dict[str, Any]:
    """Compile le manifeste legacy complet → document manifeste MCP.

    Args:
        tools:        mapping ``{name: meta}`` (issu de ``load_tools_config``) ;
        version:      version de la surface MCP ; ``None`` → ``load_mcp_version`` ;
        generated_at: horodatage ISO du catalogue ; ``None`` → maintenant UTC
            (passer une valeur fixe pour des sorties déterministes en test) ;
        source:       étiquette de provenance (documentation du catalogue) ;
        strict:       ``True`` → lève ``ManifestError`` si UNE SEULE définition
            est non conforme au standard v1 (gating CI) ; ``False`` (défaut) →
            compile et collecte les warnings.

    Returns:
        Document manifeste (tools triés alphabétiquement — sortie stable).
    """
    resolved_version = version or load_mcp_version()
    compiled: list[dict[str, Any]] = []
    warnings: list[str] = []
    for name in sorted(tools):
        try:
            entry, tool_warnings = compile_tool(name, tools[name])
        except Exception as exc:  # défensif : une entrée illisible ne tue pas le catalogue
            if strict:
                raise ManifestError(f"tool « {name} » non compilable : {exc}") from exc
            warnings.append(f"{name}: entrée illisible ({exc})")
            continue
        compiled.append(entry)
        warnings.extend(tool_warnings)
        if entry["safety"]["source"] == SAFETY_SOURCE_DEFAULT:
            warnings.append(
                f"{name}: tool non classé par la policy legacy — posture fail-closed "
                "(mutation + admin) ; déclarer « safety » (standard thinktuning.tool/v1) "
                "dans tools_config.json pour lever l'ambiguïté"
            )

    for tool_entry in compiled:
        if tool_entry["safety"]["level"] == "dangerous":
            warnings.append(
                f"{tool_entry['name']}: safety.level=« dangerous » — tool bloqué par la "
                "policy (scope admin, jamais exécuté quelle que soit la validation)"
            )
    if strict and warnings:
        raise ManifestError("manifeste non conforme (mode strict) :\n- " + "\n- ".join(warnings))

    manifest: dict[str, Any] = {
        "manifestVersion": str(resolved_version),
        "serverName": MCP_SERVER_NAME,
        "protocolVersion": MCP_PROTOCOL_VERSION,
        "source": source,
        "generatedAt": generated_at or utc_now_iso(),
        "toolCount": len(compiled),
        "readOnlyCount": sum(1 for e in compiled if e["annotations"]["readOnlyHint"]),
        "tools": compiled,
        "warnings": warnings,
    }
    logger.info(
        "Manifeste MCP compilé : %d tools (%d read-only), %d avertissement(s)",
        manifest["toolCount"],
        manifest["readOnlyCount"],
        len(warnings),
    )
    return manifest


def entry_to_mcp_tool(
    entry: Mapping[str, Any],
    handler: Callable[[dict[str, Any]], str],
    *,
    required_scope: MCPScopeRole | None = None,
) -> MCPTool:
    """Projette une entrée du manifeste → entité domaine ``MCPTool`` (tâche 6).

    Le manifeste est déclaratif (sans handler) : cette couture permet de câbler
    l'exécution sans dupliquer les métadonnées compilées (description,
    inputSchema, annotations, scope).

    Args:
        entry:          entrée produite par ``compile_tool`` / ``build_manifest`` ;
        handler:        exécution pure ``(arguments) -> str`` (lève ``ToolError``) ;
        required_scope: rôle explicite ; ``None`` → ``entry["requiredScope"]``
            (un scope inconnu lève ``ValueError`` — fail-fast domaine).
    """
    scope = required_scope or MCPScopeRole(entry.get("requiredScope", "read_only"))
    return MCPTool(
        name=str(entry.get("name", "")),
        description=str(entry.get("description", "") or ""),
        input_schema=dict(entry.get("inputSchema") or empty_input_schema()),
        annotations=dict(entry.get("annotations") or READ_ONLY_ANNOTATIONS),
        required_scope=scope,
        handler=handler,
    )


# ---------------------------------------------------------------------------
# I/O confinée (chargement du manifeste legacy, catalogue Markdown)
# ---------------------------------------------------------------------------


def _default_candidates() -> list[Path]:
    """Chemins de ``tools_config.json`` sondés sans chemin explicite.

    ``parents[3]`` remonte de ``app/infrastructure/mcp/manifest_generator.py``
    à la racine ``backend/`` (même résolution que ``version_loader``).
    """
    package_root = Path(__file__).resolve().parents[3]
    candidates = [package_root / "ia" / "tools" / _MANIFEST_FILENAME]
    cwd_candidate = Path.cwd() / "ia" / "tools" / _MANIFEST_FILENAME
    if cwd_candidate not in candidates:
        candidates.append(cwd_candidate)
    return candidates


def _fallback_empty(reason: str, strict: bool) -> dict[str, dict[str, Any]]:
    """Logge le repli (mode tolérant) ou propage l'erreur (mode strict)."""
    if strict:
        raise ManifestError(f"Manifeste legacy non résolu : {reason}")
    logger.warning("Manifeste legacy : %s — fallback sur un catalogue vide", reason)
    return {}


def load_tools_config(
    path: str | Path | None = None, *, strict: bool = False
) -> dict[str, dict[str, Any]]:
    """Charge le mapping ``{name: meta}`` depuis ``tools_config.json``.

    Args:
        path:   chemin explicite ; ``None`` → sondage racine package puis CWD ;
        strict: ``True`` → propage les erreurs (fichier absent, JSON invalide,
            structure inattendue) ; ``False`` (défaut) → warning + ``{}``.

    Returns:
        Le mapping des tools déclarés (jamais ``None`` — catalogue vide en
        tolérant, pour que le serveur MCP démarre même dégradé).
    """
    if path is not None:
        manifest_path = Path(path)
        if not manifest_path.is_file():
            return _fallback_empty(f"fichier introuvable : {manifest_path}", strict)
    else:
        found = next((c for c in _default_candidates() if c.is_file()), None)
        if found is None:
            return _fallback_empty(
                "tools_config.json introuvable (racine package et CWD sondés)", strict
            )
        manifest_path = found
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _fallback_empty(f"{manifest_path} illisible : {exc}", strict)
    tools = document.get("tools") if isinstance(document, dict) else None
    if not isinstance(tools, dict):
        return _fallback_empty(
            f"{manifest_path} : structure inattendue (objet {{\"tools\": {{...}}}} attendu)",
            strict,
        )
    return tools


def generate_manifest(
    path: str | Path | None = None,
    *,
    strict: bool = False,
    version: MCPVersion | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Chargement + compilation : ``tools_config.json`` → manifeste MCP complet."""
    tools = load_tools_config(path, strict=strict)
    return build_manifest(tools, version=version, generated_at=generated_at, strict=strict)


def _md_cell(text: str, *, max_chars: int = _MD_DESCRIPTION_MAX_CHARS) -> str:
    """Rend une cellule de tableau Markdown sûre (pipes, sauts de ligne, longueur)."""
    flat = " ".join(str(text).split())
    if len(flat) > max_chars:
        flat = flat[: max_chars - 1].rstrip() + "…"
    return flat.replace("|", "\\|") or "—"


def manifest_to_markdown(manifest: Mapping[str, Any]) -> str:
    """Rend le manifeste en catalogue produit Markdown (``docs/mcp/MANIFEST.md``).

    Sortie déterministe pour un même manifeste (aucune horloge dans le rendu :
    l'horodatage vient du document, pas de l'heure de rendu).
    """
    version = manifest.get("manifestVersion", "?")
    tool_count = manifest.get("toolCount", 0)
    read_only = manifest.get("readOnlyCount", 0)
    mutating = tool_count - read_only
    warnings = list(manifest.get("warnings") or [])

    def _flag(annotations: Mapping[str, Any], key: str) -> str:
        return "✅" if annotations.get(key) else "—"

    lines: list[str] = [
        f"# Manifeste MCP ThinkTuning — v{version}",
        "",
        "> ⚠️ **FICHIER GÉNÉRÉ** — ne pas éditer à la main.",
        f"> Source : `{manifest.get('source', _DEFAULT_SOURCE_LABEL)}` "
        "(standard `thinktuning.tool/v1`) ;",
        "> régénération : `python -m app.infrastructure.mcp.manifest_generator` "
        "(depuis `backend/`).",
        "",
        "| Attribut | Valeur |",
        "|---|---|",
        f"| Serveur | `{manifest.get('serverName', MCP_SERVER_NAME)}` |",
        f"| Version surface | {version} |",
        f"| Protocole MCP | {manifest.get('protocolVersion', MCP_PROTOCOL_VERSION)} |",
        f"| Tools | **{tool_count}** (read-only : **{read_only}** · mutation : **{mutating}**) |",
        f"| Généré le | {manifest.get('generatedAt', '—')} |",
        f"| Avertissements | {len(warnings)} |",
        "",
        "## Catalogue",
        "",
        "Posture (annotations MCP `tools/list`) : `readOnly` = readOnlyHint,",
        "`destructive` = destructiveHint, `idempotent` = idempotentHint.",
        "`Scope requis` = rôle minimal pour VOIR le tool (`MCPScopeRole`,",
        "docs/mcp/MCP_SECURITY.md) — hint design-time ; la policy runtime",
        "(tâche 5) reste le garde-fou effectif à chaque appel.",
        "",
        "| Tool | Scope requis | readOnly | destructive | idempotent | Catégorie | Description |",
        "|---|---|:-:|:-:|:-:|---|---|",
    ]
    for entry in manifest.get("tools", []):
        annotations = entry.get("annotations", {})
        lines.append(
            f"| `{entry['name']}` | {entry.get('requiredScope', 'admin')} "
            f"| {_flag(annotations, 'readOnlyHint')} | {_flag(annotations, 'destructiveHint')} "
            f"| {_flag(annotations, 'idempotentHint')} | {entry.get('category', 'builtin')} "
            f"| {_md_cell(entry.get('description', ''))} |"
        )

    lines += ["", "## Schémas d'entrée (`inputSchema`)", ""]
    for entry in manifest.get("tools", []):
        schema_json = json.dumps(entry.get("inputSchema", {}), ensure_ascii=False, indent=2)
        lines += [f"### `{entry['name']}`", "", "```json", schema_json, "```", ""]

    if warnings:
        lines += ["## Avertissements de compilation", ""]
        lines += [f"- {warning}" for warning in warnings]
        lines.append("")
    return "\n".join(lines)


def _default_catalog_path() -> Path:
    """Chemin du catalogue commité : ``<racine dépôt>/docs/mcp/MANIFEST.md``.

    ``parents[4]`` = racine du dépôt (``docs/`` vit au-dessus de ``backend/``) ;
    repli : ``backend/docs/mcp/`` si le dépôt est aplati (image Docker).
    """
    repo_root = Path(__file__).resolve().parents[4]
    if (repo_root / "docs" / "mcp").is_dir():
        return repo_root / _CATALOG_RELPATH
    return Path(__file__).resolve().parents[3] / _CATALOG_RELPATH


def write_markdown_catalog(
    output_path: str | Path | None = None, *, manifest: Mapping[str, Any] | None = None
) -> Path:
    """Régénère le catalogue produit (checklist CI : « MANIFEST.md régénéré »).

    Args:
        output_path: destination ; ``None`` → ``docs/mcp/MANIFEST.md`` du dépôt ;
        manifest:    manifeste pré-compilé ; ``None`` → ``generate_manifest()``.

    Returns:
        Le chemin du fichier écrit.
    """
    document = manifest if manifest is not None else generate_manifest()
    output = Path(output_path) if output_path else _default_catalog_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(manifest_to_markdown(document), encoding="utf-8", newline="\n")
    logger.info("Catalogue MCP régénéré : %s (%s tools)", output, document.get("toolCount", 0))
    return output


def main(argv: Sequence[str] | None = None) -> int:
    """CLI : régénère ``docs/mcp/MANIFEST.md`` depuis le manifeste legacy."""
    parser = argparse.ArgumentParser(
        description="Compile ia/tools/tools_config.json → manifeste MCP + catalogue Markdown."
    )
    parser.add_argument("--tools-config", default=None, help="Chemin de tools_config.json")
    parser.add_argument("--output", default=None, help="Chemin du catalogue Markdown")
    parser.add_argument(
        "--strict", action="store_true", help="Échoue si une définition n'est pas conforme v1"
    )
    args = parser.parse_args(argv)
    manifest = generate_manifest(args.tools_config, strict=args.strict)
    output = write_markdown_catalog(args.output, manifest=manifest)
    print(
        f"Manifeste MCP : {manifest['toolCount']} tools "
        f"({manifest['readOnlyCount']} read-only) → {output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "MUTATING_ANNOTATIONS",
    "READ_ONLY_ANNOTATIONS",
    "SAFETY_SOURCE_DECLARED",
    "SAFETY_SOURCE_DEFAULT",
    "SAFETY_SOURCE_DERIVED",
    "ManifestError",
    "ToolPosture",
    "build_manifest",
    "compile_tool",
    "entry_to_mcp_tool",
    "generate_manifest",
    "load_tools_config",
    "main",
    "manifest_to_markdown",
    "resolve_posture",
    "safety_to_annotations",
    "write_markdown_catalog",
]





