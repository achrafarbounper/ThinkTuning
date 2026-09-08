# project/app/infrastructure/mcp/legacy_tool_provider.py
"""Provider MCP des tools read-only — projection du registre legacy (S2, tâche 6).

Rec. 2 du mapping (docs/mcp/MCP_IMPLEMENTATION_MAPPING.md) : la surface MCP
remplace « GET /tools » — ce module projette le registre legacy
``ia/tools/tool_registry.py`` (TOOLS + TOOL_META depuis ``tools_config.json``)
sur le port domaine ``MCPToolRegistryPort`` (tâche 3), SANS rien réinventer :

    tools_config.json (meta legacy)
        → ``compile_tool``            entrée manifeste (inputSchema, annotations)
        → ``entry_to_mcp_tool``       entité domaine ``MCPTool`` (couture tâche 4)
        → handler délégué             ``ia.tools.tool_registry.TOOLS[name](**args)``

SÉLECTION v0.1.0 (``V010_READ_ONLY_TOOLS``) — checklist de la tâche 6 :
    ``add``, ``calc``, ``web_search``, ``web_fetch``, ``web_read``, ``http_get``,
    ``read_file``, ``list_dir``, ``find_file``, ``file_info``, ``file_checksum``,
    ``head_file``, ``count_lines``.
Le label « 12 tools » de la roadmap (docs/mcp/MCP_ROADMAP.md) arrondissait la
sélection : la checklist opérationnelle en nomme 13, toutes implémentées ici.

SÉLECTION v1.0.0 (``V100_READ_ONLY_TOOLS``, tâche 7) — extension
read-only de la roadmap (docs/mcp/MCP_ROADMAP.md) : 25 tools uniques.
L'union des deux checklists (v0.1.0 : 13 noms + tâche 7 : 13 noms)
donne 24 uniques (``file_info``/``count_lines`` en commun) ; ``touch`` est
une ÉCRITURE (jamais exposée par la surface read-only — tâche 17) ; deux
lectures pures (``read_json``/``search_in_files``) complètent le compte.
Les 5 tools métier (``job_list``/``job_get``/``model_versions``/
``dataset_stats``/``predict_sentiment``, auparavant UNKNOWN/fail-closed)
ont reçu une déclaration ``safety: safe`` (tools_config.json, tâche 7) :
chaque tool de la sélection passe par ``decide_action()`` → ``AUTO_APPROVE``
et ressort ``readOnlyHint: true`` du manifeste compilé.

GARANTIES DE SÉCURITÉ (checklist tâche 6 : « check_command_allowed +
safe_resolve + enforce_host_policy ») — portées PAR DÉLÉGATION aux
implémentations legacy, zéro duplication de règle :

    - fichiers (read_file, list_dir, find_file, file_info, file_checksum,
      head_file, count_lines) : ``ia/tools/sandbox.safe_resolve`` — aucun
      chemin ne sort de la racine sandbox (AGENT_SANDBOX_ROOT) ;
    - réseau (web_search, web_fetch, web_read, http_get) :
      ``url_scheme_allowed`` (http/https uniquement) +
      ``enforce_host_policy`` (anti-SSRF AGENT_BLOCK_PRIVATE_HOSTS) ;
    - calcul (add, calc) : fonctions PURES — calc est évalué via un AST
      whitelisté (aucun exec/eval), add est une addition ;
    - ``check_command_allowed`` (allowlist de binaires) concerne
      ``run_command``/``run_python`` — ABSENTS de la sélection read-only :
      la surface v0.1.0 n'expose AUCUN tool d'exécution.

FAIL-CLOSED à la construction :
    - un tool de la sélection dont la posture compilée n'est PAS read-only
      est EXCLU (warning) — le provider ne peut jamais exposer une mutation ;
    - un tool absent du manifeste ou sans implémentation legacy est exclu
      (warning) — aucune métadonnée synthétisée ;
    - à l'appel : arguments requis manquants ou exception legacy →
      ``ToolError`` (réponse MCP ``isError: true``, jamais un crash).

Le filtrage par SCOPE reste en infrastructure (``MCPServer._visible_tools``
et ``ScopeFilteredToolProvider``, tâche 5) : ce provider rend la vérité.
Tous les tools de la sélection exigent ``MCPScopeRole.READ_ONLY`` — visibles
de tout rôle (read_only inclus).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from typing import Any

from app.domain.entities.mcp import MCPTool
from app.domain.ports.mcp_ports import MCPToolRegistryPort
from app.infrastructure.mcp.manifest_generator import compile_tool, entry_to_mcp_tool
from app.infrastructure.mcp.mcp_server import ToolError
from ia.tools import tool_registry as _legacy_registry

logger = logging.getLogger("thinktuning.mcp.tools")

__all__ = [
    "V010_READ_ONLY_TOOLS",
    "V100_READ_ONLY_TOOLS",
    "LegacyRegistryToolProvider",
    "build_v010_read_only_provider",
    "build_v100_read_only_provider",
]

# ---------------------------------------------------------------------------
# Sélection v0.1.0 (tâche 6) — ordre alphabétique (déterministe)
# ---------------------------------------------------------------------------
V010_READ_ONLY_TOOLS: frozenset[str] = frozenset(
    {
        # calcul purs (safety « safe » déclarée dans tools_config.json — tâche 6)
        "add",
        "calc",
        # réseau / web (classification legacy NETWORK → read-only)
        "web_search",
        "web_fetch",
        "web_read",
        "http_get",
        # fichiers : lecture seule (classification legacy READ → read-only)
        "read_file",
        "list_dir",
        "find_file",
        "file_info",
        "file_checksum",
        "head_file",
        "count_lines",
    }
)

# ---------------------------------------------------------------------------
# Sélection v1.0.0 (tâche 7) — extension read-only : 25 tools uniques.
# Le label « 25 tools » de la roadmap (v1.0.0 Public Beta) est tenu ainsi :
#     - l'union des deux checklists (tâche 6 : V010 — 13 noms ; tâche  7 : 13
#       noms) donne  24 uniques — ``file_info`` et ``count_lines`` figuraient déjà
#       dans ``V010_READ_ONLY_TOOLS`` ;
#     - ``touch`` (crée / rafraîchit un fichier, cf. ``ia/tools/file_tools.py``) est
#       une ÉCRITURE (classification WRITE dure, cf. ``sandbox_policy.classify_tool``) —
#       la surface read-only n'expose JAMAIS de mutation ; il rejoindra la surface
#       write/exec à la tâche 17 ;
#     - deux lectures pures déjà classées READ (``read_json``, ``search_in_files``) — famille
#       « lecture fichiers / recherche texte » — complètent le catalogue pour tenir le
#       compte produit (25] — déjà read-only dans le manifeste compilé,zéro
#       changement de policy.

# Chaque tool de la sélection passe par ``decide_action()`` → ``AUTO_APPROVE``
# (lecture pure : READ/SYSTEM/NETWORK, cf. ``sandbox_policy.decide``) et
# ressort ``readOnlyHint: true`` du manifeste compilé (safety déclarée pour les
# 5 tools métier — ``job_list``, ``job_get``, ``model_versions``, ``dataset_stats``,
# ``predict_sentiment`` — auparavant UNKNOWN/fail-closed ; cf. tools_config.json).
# ---------------------------------------------------------------------------
V100_READ_ONLY_TOOLS: frozenset[str] = frozenset(
    # Union déterministe (set) : V010 (13) + extension v1.0.0 (12)
    V010_READ_ONLY_TOOLS
    | frozenset(
        {
            # métier ThinkTuning — lecture (jobs, modèles, dataset, sentiment)
            "job_list",
            "job_get",
            "model_versions",
            "dataset_stats",
            "predict_sentiment",
            # système / diagnostic — lecture
            "env_info",
            "disk_usage",
            "gpu_info",
            "now",
            # fichiers : lecture pure (compléments de la famille — symétriques de
            # head_file / count_lines ; lectures pures déjà classées READ)
            "tail_file",
            "read_json",
            "search_in_files",
        }
    )
)


def _result_to_text(result: Any) -> str:
    """Sérialise le retour d'un tool legacy en contenu texte MCP.

    - ``str``    : brut (read_file renvoie déjà le texte du fichier) ;
    - dict/list  : JSON indenté, non échappé (lisible par le client MCP) ;
    - scalaires  : ``str(result)`` (ex. ``add(2, 3)`` → ``5.0``).
    """
    if isinstance(result, str):
        return result
    if isinstance(result, (dict, list, tuple)):
        return json.dumps(result, ensure_ascii=False, indent=2, default=str)
    return str(result)


class LegacyRegistryToolProvider(MCPToolRegistryPort):
    """``MCPToolRegistryPort`` — projection read-only du registre legacy.

    La sélection est compilée au DÉMARRAGE (design-time) depuis les mêmes
    sources que le manifeste (``compile_tool``) : le catalogue ``tools/list``
    est bit-à-bit aligné sur ``docs/mcp/MANIFEST.md`` (zéro duplication de
    métadonnées — description, ``inputSchema``, annotations et scope viennent
    de la compilation, les handlers sont câblés par ``entry_to_mcp_tool``).

    Args:
        selection:     noms exposés (défaut : ``V100_READ_ONLY_TOOLS``) ;
        tools:         implémentations ``{name: callable}`` (défaut : ``TOOLS``
            legacy) — injectable pour les tests ;
        required_args: arguments obligatoires ``{name: [args]}`` (défaut :
            ``REQUIRED_ARGS`` dérivé de tools_config.json) ;
        manifest:      métadonnées déclaratives ``{name: meta}`` (défaut :
            ``TOOL_META`` legacy).

    Note:
        Les injections n'incluent PAS le manifeste compilé : il est TOUJOURS
        recalculé depuis ``manifest`` via ``compile_tool`` (source unique —
        le provider n'invente jamais de schéma ni d'annotations).
    """

    def __init__(
        self,
        *,
        selection: Collection[str] = V100_READ_ONLY_TOOLS,
        tools: Mapping[str, Callable[..., Any]] | None = None,
        required_args: Mapping[str, Sequence[str]] | None = None,
        manifest: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self._tools = dict(tools) if tools is not None else dict(_legacy_registry.TOOLS)
        self._required_args = (
            {name: list(args) for name, args in required_args.items()}
            if required_args is not None
            else dict(_legacy_registry.REQUIRED_ARGS)
        )
        self._manifest = (
            dict(manifest) if manifest is not None else dict(_legacy_registry.TOOL_META)
        )
        self._tool_map: dict[str, MCPTool] = {}
        self._compile_selection(sorted(selection))

    # --- Compilation (construction seule — le registre est immuable ensuite) ---

    def _compile_selection(self, names: Iterable[str]) -> None:
        """Compile et câble la sélection (fail-closed : exclusions tracées)."""
        for name in names:
            meta = self._manifest.get(name)
            if meta is None:
                logger.warning(
                    "MCP read-only : « %s » absent du manifeste legacy — exclu "
                    "(aucune métadonnée synthétisée)",
                    name,
                )
                continue
            func = self._tools.get(name)
            if func is None:
                logger.warning(
                    "MCP read-only : « %s » sans implémentation legacy — exclu", name
                )
                continue
            try:
                entry, _warnings = compile_tool(name, meta)
            except Exception as exc:  # entrée illisible : jamais bloquante
                logger.warning(
                    "MCP read-only : « %s » non compilable (%s) — exclu", name, exc
                )
                continue
            if not entry["annotations"]["readOnlyHint"]:
                # Garantie structurelle : la surface v0.1.0 est read-only.
                # Une posture mutation (reclassement, déclaration érronée…)
                # n'est JAMAIS exposée — le doute n'est pas résolu côté client.
                logger.warning(
                    "MCP read-only : « %s » résolu en posture mutation — exclu "
                    "de la surface read-only (voir docs/mcp/MANIFEST.md)",
                    name,
                )
                continue
            self._tool_map[name] = entry_to_mcp_tool(
                entry, self._wrap_handler(name, func)
            )

    def _wrap_handler(
        self, name: str, func: Callable[..., Any]
    ) -> Callable[[dict[str, Any]], str]:
        """Câble l'exécution legacy : validation args → appel → sérialisation."""

        def handler(arguments: dict[str, Any]) -> str:
            args = dict(arguments or {})
            required = self._required_args.get(name, [])
            missing = [arg for arg in required if arg not in args]
            if missing:
                raise ToolError(
                    f"Argument(s) requis manquant(s) pour « {name} » : "
                    f"{', '.join(missing)}"
                )
            try:
                result = func(**args)
            except ToolError:
                raise
            except Exception as exc:  # FileNotFoundError, PermissionError, SSRF…
                # Erreur MÉTIER (isError: true) : le client MCP raisonne dessus
                # (ex. read_file suggère find_file), le transport reste sain.
                raise ToolError(f"{name} a échoué : {type(exc).__name__} : {exc}") from exc
            return _result_to_text(result)

        return handler

    # --- MCPToolRegistryPort ----------------------------------------------------

    def list_tools(self) -> list[MCPTool]:
        """Tous les tools read-only de la sélection (la vérité, sans filtre)."""
        return list(self._tool_map.values())

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Exécute un tool de la sélection par son nom (délégation legacy)."""
        tool = self._tool_map.get(name)
        if tool is None:
            # Indiscernable d'un tool absent : aucun oracle d'inventaire.
            raise ToolError(f"Unknown tool: {name}")
        return tool.handler(dict(arguments or {}))


def build_v010_read_only_provider() -> LegacyRegistryToolProvider:
    """Provider de la surface v0.1.0 (sélection explicite — 13 tools).

    La surface PAR DÉFAUT du serveur MCP est la v1.0.0
    (``build_v100_read_only_provider``, tâche 7) — ce builder reste
    disponible pour les déploiements restreints / tests v0.1.0.
    """
    return LegacyRegistryToolProvider(selection=V010_READ_ONLY_TOOLS)


def build_v100_read_only_provider() -> LegacyRegistryToolProvider:
    """Provider par défaut de la surface v1.0.0 (25 tools read-only, tâche 7)."""
    return LegacyRegistryToolProvider()
