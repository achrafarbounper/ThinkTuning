# project/app/infrastructure/mcp/admin_tool_provider.py
"""Provider MCP des tools admin — extension mutante complète (S7, tâche 19).

La surface v1.0.0 (``LegacyRegistryToolProvider``, tâche 7) n'expose QUE des
tools read-only et la v2.1.0 (``WriteExecToolProvider``, tâche 17) QUE du
write/exec filtré (35 tools legacy). La tâche 19 (S7, roadmap v2.2.0 — compte
roadmap v3.0.0 « 40 tools ») ouvre les **5 derniers tools mutatifs** du
registre legacy (``ia/tools/tools_config.json`` + ``ia/tools/tool_registry.py``)
via un provider COMPLÉMENTAIRE, même mécanique de fail-closed INVERSÉ :

    tools_config.json (meta legacy)
        → ``compile_tool``            entrée manifeste (inputSchema, annotations)
        → ``entry_to_mcp_tool``       entité domaine ``MCPTool`` (+ scope ADMIN)
        → handler délégué             ``ia.tools.tool_registry.TOOLS[name](**args)``

SÉLECTION v2.2.0 (``V220_ADMIN_TOOLS``) — checklist de la tâche 19
(docs/mcp/IMPLEMENTATION_PLAN.md) :

    move_path, remove_path        (fichiers : déplacement / suppression)
    split_file, dedupe_lines      (fichiers : production avancée)
    unzip_file                    (archives)

(5 tools mutatifs ; union read-only (25, tâche 7) + write/exec (10, tâche 17)
+ admin (5) = **40 tools** — catalogue complet, compte roadmap v3.0.0
« MCP-First » — docs/mcp/MCP_ROADMAP.md.)

GARANTIES DE SÉCURITÉ (checklist tâche 19 : « decide_action() +
MCPSecurityScope ») :
    - chaque tool de la sélection compile en posture **MUTATION**
      (``destructiveHint: true`` / ``idempotentHint: false`` / ``readOnlyHint:
      false`` — ``sandbox_policy.classify_tool`` : WRITE pour move_path /
      split_file / dedupe_lines / unzip_file, DELETE pour remove_path) — un
      tool résolu en lecture est EXCLU à la construction ;
    - le scope d'exposition est ``MCPScopeRole.ADMIN`` pour les 5 tools :
      aligné sur le catalogue par rôle (``scope_enforcer.ADMIN_ROLE_TOOLS``
      = 40 tools = 25 read-only + 10 write/exec + ces 5) et sur l'échelle de
      privilège MCP (docs/mcp/MCP_SECURITY.md : `admin` (40 tools)) — le
      ``remove_path`` (DELETE) et l'extraction d'archives ne sont JAMAIS
      exposés à un rôle inférieur ;
    - à l'appel, chaque tool passe par ``sandbox_policy.decide_action()`` :
      verdict ``APPROVE`` (mutation sur cible non sensible) → **validation
      humaine obligatoire** — le ``PolicyGateToolProvider`` (policy_adapter,
      tâche 5, actif dès la v2.1.0) enveloppe la surface : un appel direct ne
      peut JAMAIS atteindre l'implémentation legacy sans approbation ;
    - ``decide_action()`` conserve aussi ses **règles dures** (chemins
      sensibles ``.git``/``.env``… → REJECT, jamais exécuté, audité) ;
    - la visibilité reste doublement filtrée : ``MCPServer._visible_tools``
      (``MCPScopeRole.granted``) côté transport ET ``MCPScopeEnforcer`` côté
      client (``MCPSecurityScope`` → catalogue du rôle / whitelist
      ``visible_tools``, tâche 11).

FAIL-CLOSED à la construction :
    - outil absent du manifeste legacy, sans implémentation, non compilable,
      ou résolu en posture lecture → EXCLU (warning tracé), jamais un stub ;
    - à l'appel : argument requis manquant ou exception legacy → ``ToolError``
      (réponse MCP ``isError: true``, jamais un crash du transport).

Le filtrage par SCOPE reste en infrastructure (``MCPServer._visible_tools``) :
ce provider rend la vérité (5 tools, scope ADMIN), le serveur projette la vue
sécurisée. Hérite de ``LegacyRegistryToolProvider`` (``list_tools`` /
``call_tool`` / ``_wrap_handler`` identiques) — seul le ``_compile_selection``
est inversé (garder mutation, exclure lecture) et le scope forcé à ADMIN.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from typing import Any

from app.domain.entities.mcp import MCPScopeRole
from app.infrastructure.mcp.legacy_tool_provider import LegacyRegistryToolProvider
from app.infrastructure.mcp.manifest_generator import compile_tool, entry_to_mcp_tool

logger = logging.getLogger("thinktuning.mcp.admin")

# Checklist exacte de la tâche 19 (docs/mcp/IMPLEMENTATION_PLAN.md).
V220_ADMIN_TOOLS: frozenset[str] = frozenset(
    {
        "move_path",
        "remove_path",
        "split_file",
        "dedupe_lines",
        "unzip_file",
    }
)

# Scope d'exposition de la surface admin : ADMIN — catalogue par rôle
# (``ADMIN_ROLE_TOOLS`` = 40 tools : 25 read-only + 10 write/exec + ces 5,
# tâche 19). Même quand le manifeste dérive un scope différent, l'exposition
# MCP de la sélection reste pilotée par la roadmap (v2.2.0 → compte 40).
ADMIN_TOOLS_SCOPE = MCPScopeRole.ADMIN


class AdminToolProvider(LegacyRegistryToolProvider):
    """``MCPToolRegistryPort`` — projection admin du registre legacy (tâche 19).

    Même mécanique que ``LegacyRegistryToolProvider`` et
    ``WriteExecToolProvider`` (compilation design-time du manifeste legacy via
    ``compile_tool``, handlers câblés par délégation aux implémentations
    legacy) avec le fail-closed INVERSÉ du write/exec :

        - read-only (provider tâche 6/7)   : posture LECTURE exigée → un tool
          mutation est exclu (la surface v0.1.0/v1.0.0 ne mute JAMAIS) ;
        - write/exec (provider tâche 17)   : posture MUTATION exigée → un tool
          lecture est exclu (la surface v2.1.0 n'expose que du filtré) ;
        - admin     (ce provider, tâche 19): idem write/exec, scope ADMIN —
          les 5 derniers tools mutatifs (déplacement / suppression / archives).

    Args (identiques au provider legacy) :
        selection:     noms exposés (défaut : ``V220_ADMIN_TOOLS``) ;
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
        selection: Collection[str] = V220_ADMIN_TOOLS,
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
                    "MCP admin : « %s » absent du manifeste legacy — exclu "
                    "(aucune métadonnée synthétisée)",
                    name,
                )
                continue
            func = self._tools.get(name)
            if func is None:
                logger.warning(
                    "MCP admin : « %s » sans implémentation legacy — exclu", name
                )
                continue
            try:
                entry, _warnings = compile_tool(name, meta)
            except Exception as exc:  # entrée illisible : jamais bloquante
                logger.warning(
                    "MCP admin : « %s » non compilable (%s) — exclu", name, exc
                )
                continue
            if entry["annotations"]["readOnlyHint"]:
                # Garantie structurelle : la surface admin est MUTANTE.
                # Une posture lecture (déclaration `safety: safe`, reclassement
                # du registre…) n'est JAMAIS exposée ici — le doute n'est pas
                # résolu côté client (miroir des providers read-only/write-exec).
                logger.warning(
                    "MCP admin : « %s » résolu en posture lecture — exclu "
                    "de la surface admin (voir docs/mcp/MANIFEST.md)",
                    name,
                )
                continue
            self._tool_map[name] = entry_to_mcp_tool(
                entry,
                self._wrap_handler(name, func),
                required_scope=ADMIN_TOOLS_SCOPE,
            )


def build_v220_admin_provider() -> AdminToolProvider:
    """Provider de la surface admin v2.2.0 (5 tools mutatifs, tâche 19).

    La surface PAR DÉFAUT du serveur MCP expose cette extension à partir de la
    v2.2.0 (``build_mcp_server``) — ce builder reste disponible pour les
    déploiements restreints / tests unitaires.
    """
    return AdminToolProvider()


__all__ = [
    "ADMIN_TOOLS_SCOPE",
    "AdminToolProvider",
    "V220_ADMIN_TOOLS",
    "build_v220_admin_provider",
]
