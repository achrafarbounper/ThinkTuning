# project/app/infrastructure/mcp/mcp_server_factory.py
"""Fabrique du serveur MCP — construction avec scope de sécurité (S1, tâche 2).

Le bootstrap S1 livre un registre de tools EN MÉMOIRE (``InMemoryToolProvider``)
avec deux tools de démonstration/contrat :

    - ``mcp_version``  : version de la surface MCP (read-only) ;
    - ``server_info``  : identité du serveur MCP (read-only).

La surface v1.0.0 (tâche  7) est la projection du registre legacy sur le port
domaine ``MCPToolRegistryPort`` : ``LegacyRegistryToolProvider`` (tâche  6)
expose la sélection read-only ``V100_READ_ONLY_TOOLS`` (25 tools read-only : la
checklist v0.1.0, 13 nommés, tâche  6, plus extension read-only tâche  7) —
toutes implémentées ici (cf. ``legacy_tool_provider.py``). Les deux tools bootstrap S1
restent exposés à côté :

    - ``mcp_version``  : version de la surface MCP (read-only) ;
    - ``server_info``  : identité du serveur MCP (read-only).

Tâche 8 (S3) : ``build_mcp_server()`` branche aussi le registre des RESOURCES
``thinktuning://`` (``LegacyResourceProvider``) ; ``resource_provider=...``
le remplace entièrement (tests, déploiements restreints).

Tâche 9 (S3) : ``build_mcp_server()`` branche aussi le registre des PROMPTS
(``PromptProvider`` — 5 prompts : analyze-sentiment, plan-training,
summarize-job, compare-models, explain-prediction — tâches 9 + 14) ;
``prompt_provider=...`` le remplace entièrement (tests, déploiements
restreints).

Tâche 13 (S5, v1.1.0) : la surface resources est étendue à 10 (4 statiques +
6 paramétrées : logs de job, métadonnées de modèle, aperçu de dataset,
métriques d'entraînement, santé) — même provider, résolution par routes
regex ancrées.

Le registre par défaut est donc l'UNION (bootstrap + sélection read-only) ;
``build_mcp_server(tool_provider=...)`` le remplace entièrement (tests,
déploiements restreints). Le scope (rôle) est fixé à la construction :
``build_mcp_server(scope=...)`` filtre la visibilité des tools (fail-closed
via ``MCPScopeRole.granted``).

La version MCP est résolue par ``load_mcp_version`` (tâche 1) — le serveur
démarre même si ``pyproject.toml`` est absent (fallback ``DEFAULT_MCP_VERSION``).

Tâche 16 (S6, v2.0.0) : le tool ``orchestrate`` (orchestration agentique —
wrap d'``AgentCore.run`` via ``build_agent_core``) est ajouté au registre par
défaut comme tool MCP DISTINCT des tools bruts, à partir de la version 2.0.0
de la surface (``version >= MCPVersion(2, 0, 0)``) ; ``orchestrate_tool=...``
le remplace entièrement (tests, déploiements restreints).

Tâche 17 (S6, v2.1.0 roadmap) : l'extension write/exec (``WriteExecToolProvider``
— 10 tools mutatifs : écrire/copier dans la sandbox, exécuter, piloter les
entraînements) rejoint le registre par défaut à partir de la v2.1.0 →
35 tools (25 read-only + 10 write/exec). Sur cette surface, le registre est
enveloppé par ``PolicyGateToolProvider`` : chaque ``tools/call`` passe par
``sandbox_policy.decide_action()`` — APPROVE (mutation) → validation humaine
exigée, REJECT → refus, AUTO_APPROVE (lecture et introspection) → exécution.
MCP n'est PAS un bypass de la security interne (docs/mcp/MCP_SECURITY.md).

Tâche 19 (S7, v2.2.0 roadmap — compte v3.0.0 « 40 tools ») : l'extension admin
(``AdminToolProvider`` — 5 tools mutatifs : move_path, remove_path, split_file,
dedupe_lines, unzip_file) rejoint le registre par défaut à partir de la v2.2.0
→ **40 tools legacy** (25 read-only + 10 write/exec + 5 admin = catalogue
complet), en scope ``MCPScopeRole.ADMIN`` (aligné sur
``scope_enforcer.ADMIN_ROLE_TOOLS`` = 40). La surface reste enveloppée par le
``PolicyGateToolProvider`` : les 5 nouveaux tools passent par
``decide_action()`` (APPROVE → validation humaine ; cibles sensibles → REJECT)
et restent invisibles pour tout rôle < admin.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.domain.entities.mcp import MCPScopeRole, MCPTool, MCPVersion
from app.domain.ports.mcp_ports import (
    MCPPromptRegistryPort,
    MCPResourceRegistryPort,
    MCPToolRegistryPort,
    SamplingPort,
)
from app.infrastructure.mcp.admin_tool_provider import build_v220_admin_provider
from app.infrastructure.mcp.legacy_tool_provider import build_v100_read_only_provider
from app.infrastructure.mcp.mcp_server import (
    InMemoryToolProvider,
    MCPServer,
    ToolProvider,
)
from app.infrastructure.mcp.policy_adapter import PolicyGateToolProvider
from app.infrastructure.mcp.prompts.prompt_provider import build_prompt_provider
from app.infrastructure.mcp.protocol import MCP_SERVER_NAME, empty_input_schema
from app.infrastructure.mcp.resources.resource_provider import (
    build_legacy_resource_provider,
)
from app.infrastructure.mcp.sampling.sampling_adapter import build_sampling_adapter
from app.infrastructure.mcp.tools.orchestrate_tool import build_orchestrate_tool
from app.infrastructure.mcp.version_loader import load_mcp_version
from app.infrastructure.mcp.write_exec_tool_provider import build_v210_write_exec_provider

logger = logging.getLogger("thinktuning.mcp.factory")


def _bootstrap_tools(version: MCPVersion) -> list[MCPTool]:
    """Tools de contrat du bootstrap S1 (read-only, scope minimal)."""
    version_text = str(version)

    def _mcp_version(_: dict) -> str:
        return version_text

    def _server_info(_: dict) -> str:
        return f"{MCP_SERVER_NAME}@{version_text}"

    return [
        MCPTool(
            name="mcp_version",
            description=(
                "Version de la surface MCP ThinkTuning (feuille de route "
                "docs/mcp/IMPLEMENTATION_PLAN.md)."
            ),
            input_schema=empty_input_schema(),
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
            required_scope=MCPScopeRole.READ_ONLY,
            handler=_mcp_version,
        ),
        MCPTool(
            name="server_info",
            description="Identité du serveur MCP (nom + version).",
            input_schema=empty_input_schema(),
            annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
            required_scope=MCPScopeRole.READ_ONLY,
            handler=_server_info,
        ),
    ]


def build_mcp_server(
    scope: MCPScopeRole = MCPScopeRole.READ_ONLY,
    *,
    version: MCPVersion | None = None,
    tool_provider: ToolProvider | None = None,
    resource_provider: MCPResourceRegistryPort | None = None,
    prompt_provider: MCPPromptRegistryPort | None = None,
    sampling_port: SamplingPort | None = None,
    audit: Callable[..., Any] | None = None,
    orchestrate_tool: MCPTool | None = None,
) -> MCPServer:
    """Construit un ``MCPServer`` prêt à l'emploi pour un transport.

    Args:
        scope:          rôle du client (docs/mcp/MCP_SECURITY.md) — filtre la
            visibilité des tools exposés par ListTools/CallTool ;
        version:        version de la surface MCP ; ``None`` → résolue via
            ``load_mcp_version`` (pyproject.toml ``[tool.mcp] version``) ;
        tool_provider:  source de vérité des tools ; ``None`` → registre par
            défaut (bootstrap S1 ``mcp_version``/``server_info`` + sélection
            read-only v1.0.0 du registre legacy (25 tools, tâche  7).
        resource_provider: source des resources ``thinktuning://`` ;
            ``None`` → registre par défaut (``LegacyResourceProvider``,
            tâches 8 + 13 : 10 resources). Passer un provider VIDE
            (``list_resources()`` nulle) pour une surface supportant MCP
            resources sans en lister aucune.
        prompt_provider: source des prompts MCP ; ``None`` → registre par
            défaut (``PromptProvider``, tâches 9 + 14 : 5 prompts —
            analyze-sentiment, plan-training, summarize-job, compare-models,
            explain-prediction). Passer un provider VIDE
            (``list_prompts()`` nulle) pour une surface supportant MCP
            prompts sans en lister aucun.
        audit: hook d'audit des appels MCP (tâche 12) ; ``None`` → aucune
            écriture. Les transports passent ``mcp_audit.audit_mcp_call`` —
            chaque tools/call, resources/read, prompts/get et sampling/create
            est alors journalisé dans ``agent_audit`` (subject=client_id).
        orchestrate_tool: tool ``orchestrate`` (S6, tâche 16) à exposer sur la
            surface v2.0.0+ ; ``None`` → ``build_orchestrate_tool()`` (noyau
            agentique réel, construit paresseusement à l'appel). Ignoré si
            ``version < 2.0.0`` (jalon de la feuille de route : le tool n'est
            pas annoncé avant la v2.0.0) ou si ``tool_provider`` est fourni
            (le provider remplace entièrement le registre par défaut).
        sampling_port: port ``SamplingPort`` (S6, tâche 15) — reverse LLM
            inference (``sampling/create``) ; ``None`` →
            ``build_sampling_adapter()`` pour v2.0.0+ (sampling activé par
            défaut), ``None`` pour < v2.0.0 (pas de sampling). Passer un
            port explicite pour injecter un LLM custom (tests, déploiements).

    Returns:
        Un ``MCPServer`` configuré (dispatch JSON-RPC, prêt pour SSE/stdio).
    """
    resolved_version = version or load_mcp_version()
    provider: MCPToolRegistryPort
    if tool_provider is None:
        # Surface v2.1.0 (tâche 17) : bootstrap S1 + sélection read-only v1.0.0
        # + extension write/exec (10 tools) + orchestrate (tâche 16).
        legacy = build_v100_read_only_provider()
        tools = [*_bootstrap_tools(resolved_version), *legacy.list_tools()]
        if resolved_version >= MCPVersion(major=2, minor=1, patch=0):
            # Tâche 17 (S6, v2.1.0 roadmap) : les 10 tools write/exec (écriture
            # sandbox, exécution, pilotage d'entraînement) rejoignent la surface
            # par défaut — 35 tools (25 read-only + 10 write/exec).
            write_exec = build_v210_write_exec_provider()
            tools.extend(write_exec.list_tools())
        if resolved_version >= MCPVersion(major=2, minor=2, patch=0):
            # Tâche 19 (S7, v2.2.0 → compte roadmap v3.0.0 « 40 tools ») : les
            # 5 tools admin (move_path, remove_path, split_file, dedupe_lines,
            # unzip_file) rejoignent la surface par défaut — 40 tools legacy
            # (25 read-only + 10 write/exec + 5 admin), catalogue COMPLET.
            # Scope ADMIN : `remove_path` (DELETE) et l'extraction d'archives
            # ne sont JAMAIS exposés à un rôle inférieur (fail-closed serveur).
            admin = build_v220_admin_provider()
            tools.extend(admin.list_tools())
        if resolved_version >= MCPVersion(major=2, minor=0, patch=0):
            # Tâche 16 (S6, v2.0.0) : le tool ``orchestrate`` (orchestration
            # agentique, wrap d'AgentCore.run) est ajouté comme tool MCP
            # DISTINCT des tools bruts — visible dès la v2.0.0 de la surface.
            # ``orchestrate_tool=...`` permet d'injecter une variante (tests,
            # déploiements) ; la construction du noyau reste paresseuse (à
            # l'appel du tool), le serveur démarre sans LLM ni registre réels.
            tool = orchestrate_tool if orchestrate_tool is not None else build_orchestrate_tool()
            tools.append(tool)
        base_provider = InMemoryToolProvider(tools)
        if resolved_version >= MCPVersion(major=2, minor=1, patch=0):
            # Gate de policy actif dès que la surface expose des mutations
            # (tâche 17) : chaque tools/call passe par decide_action() —
            # AUTO_APPROVE → exécution ; APPROVE (write/exec) → validation
            # humaine exigée ; REJECT (règle dure : chemin sensible…) → refus.
            # Les tools bootstrap/orchestrate sont classés SYSTEM (AUTO_APPROVE)
            # : leur sécurité est portée par leurs couches propres.
            provider = PolicyGateToolProvider(base_provider)
        else:
            provider = base_provider
    else:
        provider = tool_provider
    if resource_provider is None:
        # Tâches 8 + 13 : les 10 resources thinktuning:// — construction SANS
        # I/O ni import lourd (sources internes résolues paresseusement à la
        # lecture).
        resource_provider = build_legacy_resource_provider()
    if prompt_provider is None:
        # Tâches 9 + 14 : les 5 prompts ThinkTuning — catalogue statique, sans I/O.
        prompt_provider = build_prompt_provider()
    if sampling_port is None:
        # Tâche 15 (S6, v2.0.0) : reverse LLM inference — le port est résolu
        # automatiquement pour v2.0.0+ (sampling activé par défaut) et reste
        # ``None`` pour < v2.0.0 (pas de capacité sampling, fail-closed).
        if resolved_version >= MCPVersion(major=2, minor=0, patch=0):
            sampling_port = build_sampling_adapter()
    server = MCPServer(
        name=MCP_SERVER_NAME,
        version=resolved_version,
        scope=scope,
        tool_provider=provider,
        resource_provider=resource_provider,
        prompt_provider=prompt_provider,
        sampling_port=sampling_port,
        audit=audit,
    )
    logger.info(
        "Serveur MCP construit : %s@%s (scope=%s, tools=%d, resources=%d, prompts=%d)",
        server.name,
        resolved_version,
        scope.value,
        len(provider.list_tools()),
        len(resource_provider.list_resources()),
        len(prompt_provider.list_prompts()),
    )
    return server


__all__ = ["build_mcp_server"]
