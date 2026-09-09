# project/app/infrastructure/mcp/write_exec_tool_provider.py
"""Provider MCP des tools write/exec — extension écrite/mutante de la surface (S6, tâche 17).

La surface v1.0.0 (``LegacyRegistryToolProvider``, tâche 7) n'expose QUE des
tools read-only : sa construction exclut structurellement toute posture
mutation. La tâche 17 ouvre la surface **write/exec filtrée** du registre
legacy (``ia/tools/tools_config.json`` + ``ia/tools/tool_registry.py``) via un
provider COMPLÉMENTAIRE, avec un fail-closed INVERSÉ :

    tools_config.json (meta legacy)
        → ``compile_tool``            entrée manifeste (inputSchema, annotations)
        → ``entry_to_mcp_tool``       entité domaine ``MCPTool`` (+ scope OPERATOR)
        → handler délégué             ``ia.tools.tool_registry.TOOLS[name](**args)``

SÉLECTION v2.1.0 (``V210_WRITE_EXEC_TOOLS``) — checklist de la tâche 17
(docs/mcp/IMPLEMENTATION_PLAN.md) :

    write_file, write_json, append_file, make_dir, copy_path   (écriture sandbox)
    run_command, run_python                                    (exécution)
    start_training, cancel_training, stop_training             (pilotage ML)

(10 tools mutatifs ; avec les 25 read-only de la v1.0.0 → **35 tools**, compte
de la roadmap v2.1.0 « Orchestrate » — docs/mcp/MCP_ROADMAP.md.)

GARANTIES DE SÉCURITÉ (checklist tâche 17 : « write/exec filtré ») :
    - chaque tool de la sélection compile en posture **MUTATION**
      (``destructiveHint: true`` / ``idempotentHint: false`` / ``readOnlyHint:
      false``) — un tool résolu en lecture (déclaration ``safety: safe``…)
      est EXCLU à la construction (le doute n'est jamais résolu côté client) ;
    - le scope d'exposition est ``MCPScopeRole.OPERATOR`` pour les 10 tools :
      aligné sur le catalogue par rôle (``scope_enforcer.OPERATOR_ROLE_TOOLS``
      = 35 tools = 25 read-only + ces 10) et sur l'échelle de privilège MCP
      (docs/mcp/MCP_SECURITY.md : `operator` (35 tools + exec filtré)) ;
    - à l'appel, chaque tool passe par ``sandbox_policy.decide_action()`` :
      verdict ``APPROVE`` (write/exec sur cible non sensible) → **validation
      humaine obligatoire** — le ``PolicyGateToolProvider`` (policy_adapter,
      tâche 5) est câblé par ``build_mcp_server`` dès que la surface expose
      ces mutations (v2.1.0+) : un appel direct ne peut JAMAIS atteindre
      l'implémentation legacy sans approbation ;
    - ``decide_action()`` conserve aussi ses **règles dures** (chemins
      sensibles ``.git``/``.env``… → REJECT, jamais exécuté, audité).

FAIL-CLOSED à la construction :
    - outil absent du manifeste legacy, sans implémentation, non compilable,
      ou résolu en posture lecture → EXCLU (warning tracé), jamais un stub ;
    - à l'appel : argument requis manquant ou exception legacy → ``ToolError``
      (réponse MCP ``isError: true``, jamais un crash du transport).

Le filtrage par SCOPE reste en infrastructure (``MCPServer._visible_tools``) :
ce provider rend la vérité (10 tools, scope OPERATOR), le serveur projette la
vue sécurisée. Hérite de ``LegacyRegistryToolProvider`` (``list_tools`` /
``call_tool`` / ``_wrap_handler`` identiques) — seul le ``_compile_selection``
est inversé (garder mutation, exclure lecture) et le scope forcé à OPERATOR.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from typing import Any

from app.domain.entities.mcp import MCPScopeRole
from app.infrastructure.mcp.legacy_tool_provider import LegacyRegistryToolProvider
from app.infrastructure.mcp.manifest_generator import compile_tool, entry_to_mcp_tool

logger = logging.getLogger("thinktuning.mcp.write_exec")

# Checklist exacte de la tâche 17 (docs/mcp/IMPLEMENTATION_PLAN.md).
V210_WRITE_EXEC_TOOLS: frozenset[str] = frozenset(
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

# Scope d'exposition de la surface write/exec : OPERATOR — catalogue par rôle
# (``OPERATOR_ROLE_TOOLS`` = 35 tools : 25 read-only + ces 10, tâche 17).
# Même quand le manifeste dérive un scope différent (ex. UNKNOWN → admin),
# l'exposition MCP de la sélection reste pilotée par la roadmap v2.1.0.
WRITE_EXEC_TOOLS_SCOPE = MCPScopeRole.OPERATOR


class WriteExecToolProvider(LegacyRegistryToolProvider):
    """``MCPToolRegistryPort`` — projection write/exec du registre legacy (tâche 17).

    Même mécanique que ``LegacyRegistryToolProvider`` (compilation design-time
    du manifeste legacy via ``compile_tool``, handlers câblés par délégation
    aux implémentations legacy) avec un fail-closed INVERSÉ :

        - read-only  (provider tâche 6/7) : posture LECTURE exigée → un tool
          mutation est exclu (la surface v0.1.0/v1.0.0 ne mute JAMAIS) ;
        - write/exec (ce provider)        : posture MUTATION exigée → un tool
          lecture est exclu (la surface v2.1.0 n'expose que du filtré).

    Args (identiques au provider legacy) :
        selection:     noms exposés (défaut : ``V210_WRITE_EXEC_TOOLS``) ;
        tools:         implémentations ``{name: callable}`` (défaut : ``TOOLS``
            legacy) — injectable pour les tests ;
        required_args: arguments obligatoires ``{name: [args]}`` (défaut :
            ``REQUIRED_ARGS`` dérivé de tools_config.json) ;
        manifest:      métadonnées déclaratives ``{name: meta}`` (défaut :
            ``TOOL_META`` legacy).
    """

    def __init__(
        self,
        *,
        selection: Collection[str] = V210_WRITE_EXEC_TOOLS,
        tools: Mapping[str, Callable[..., Any]] | None = None,
        required_args: Mapping[str, Sequence[str]] | None = None,
        manifest: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        super().__init__(
            selection=selection,
            tools=tools,
            required_args=required_args,
            manifest=manifest,
        )

    # --- Compilation (construction seule — le registre est immuable ensuite) ---

    def _compile_selection(self, names: Iterable[str]) -> None:
        """Compile et câble la sélection (fail-closed : exclusions tracées)."""
        for name in names:
            meta = self._manifest.get(name)
            if meta is None:
                logger.warning(
                    "MCP write/exec : « %s » absent du manifeste legacy — exclu "
                    "(aucune métadonnée synthétisée)",
                    name,
                )
                continue
            func = self._tools.get(name)
            if func is None:
                logger.warning(
                    "MCP write/exec : « %s » sans implémentation legacy — exclu",
                    name,
                )
                continue
            try:
                entry, _warnings = compile_tool(name, meta)
            except Exception as exc:  # entrée illisible : jamais bloquante
                logger.warning(
                    "MCP write/exec : « %s » non compilable (%s) — exclu", name, exc
                )
                continue
            if entry["annotations"]["readOnlyHint"]:
                # Garantie structurelle : la surface write/exec est MUTANTE.
                # Une posture lecture (déclaration `safety: safe`, reclassement
                # du registre…) n'est JAMAIS exposée ici — le doute n'est pas
                # résolu côté client (miroir du fail-closed du provider lisible).
                logger.warning(
                    "MCP write/exec : « %s » résolu en posture lecture — exclu "
                    "de la surface write/exec (voir docs/mcp/MANIFEST.md)",
                    name,
                )
                continue
            self._tool_map[name] = entry_to_mcp_tool(
                entry,
                self._wrap_handler(name, func),
                required_scope=WRITE_EXEC_TOOLS_SCOPE,
            )


def build_v210_write_exec_provider() -> WriteExecToolProvider:
    """Provider de la surface write/exec v2.1.0 (10 tools mutatifs, tâche 17).

    La surface PAR DÉFAUT du serveur MCP expose cette extension à partir de la
    v2.1.0 (``build_mcp_server``) — ce builder reste disponible pour les
    déploiements restreints / tests unitaires.
    """
    return WriteExecToolProvider()


__all__ = [
    "V210_WRITE_EXEC_TOOLS",
    "WRITE_EXEC_TOOLS_SCOPE",
    "WriteExecToolProvider",
    "build_v210_write_exec_provider",
]
