# project/app/infrastructure/mcp/tools/orchestrate_tool.py
"""Tool MCP `orchestrate` — orchestration agentique complète (S6, tâche 16).

Un SEUL tool vs les tools bruts : au lieu d'exposer 25 handlers read-only,
``orchestrate`` encapsule la boucle agentique du nouveau noyau
(``AgentCore.run``, ``app/agent/core.py``) : le client MCP fournit une demande
libre (``prompt``) et l'agent PLANIFIE, appelle ses outils internes et répond.

Câblage (docs/mcp/MCP_IMPLEMENTATION_MAPPING.md) :

    tools/call orchestrate (arguments = prompt / session_id / scope)
        -> ``orchestrate()``              wrapper pur de ce module
        -> ``build_agent_core()``         composition root réelle (app/agent/factory.py)
        -> ``AgentCore.run(Intent(prompt, session_id, role=scope))``
        -> ``AgentRunResult``             answer + actions (traces) + budget

SÉCURITÉ (orchestrate n'est JAMAIS un bypass de la policy de sandbox) :
    - chaque action du run passe par ``sandbox_policy.decide_action()``
      (``AgentCore._execute_plan``) :
        - AUTO_APPROVE (lecture/system)  -> exécution immédiate ;
        - APPROVE (write/delete/exec/UNKNOWN = mutation) -> run en
          ``pending_approval`` avec l'action en attente (``awaiting_action``),
          surfacee par le tool via ``awaiting_approval: true`` — la validation
          humaine suit le flux ``core/approval_store`` existant ;
        - REJECT (règle dure : cible sensible…) -> refus audité, jamais exécuté ;
    - le tool ``orchestrate`` est déclaré MUTATION (``MUTATING_ANNOTATIONS``)
      et exige un rôle ``CONTRIBUTOR``+ : il peut déclencher des écritures,
      toutes soumises à validation humaine. Il reste un tool MCP DISTINCT des
      tools bruts : jamais acheminé via ``LegacyRegistryToolProvider``.

Testabilité : la fabrique du noyau (``core_factory``) est injectable — la
production utilise ``build_agent_core`` (import paresseux à l'appel : le socle
MCP reste léger à l'import), les tests branchent un ``AgentCore`` sur des
fakes LLM/registre (mêmes fakes que ``tests/test_agent_core.py``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from app.agent.core import AgentCore, AgentRunResult
from app.domain.entities.mcp import MCPScopeRole, MCPTool
from app.domain.entities.plan import Intent
from app.infrastructure.mcp.manifest_generator import MUTATING_ANNOTATIONS
from app.infrastructure.mcp.mcp_server import ToolError

logger = logging.getLogger("thinktuning.mcp.orchestrate")

__all__ = [
    "ORCHESTRATE_TOOL_NAME",
    "build_orchestrate_tool",
    "orchestrate",
]

# Identifiant MCP du tool — tranche AUSSI l'action d'audit dans
# ``MCPServer._audit_method`` (``ACT_MCP_ORCHESTRATE`` vs ``ACT_MCP_TOOL_CALL``).
ORCHESTRATE_TOOL_NAME = "orchestrate"

# Valeurs par défaut des arguments optionnels du tool (TOUJOURS sérialisables).
_DEFAULT_SESSION_ID = "default"
_DEFAULT_SCOPE = "default"


def _default_agent_core() -> AgentCore:
    """Fabrique RÉELLE du noyau agentique (import paresseux, socle MCP léger).

    ``app.agent.factory.build_agent_core`` assemble le ``AgentCore`` complet
    (client LLM réel + registre legacy) depuis ``app/config/settings.py`` — il
    ne doit être importé qu'au moment de l'appel (jamais à l'import du module).
    """
    from app.agent.factory import build_agent_core

    return build_agent_core()


def orchestrate(
    prompt: str,
    session_id: str = _DEFAULT_SESSION_ID,
    scope: str = _DEFAULT_SCOPE,
    *,
    core_factory: Callable[[], AgentCore] | None = None,
) -> AgentRunResult:
    """Exécute un run agentique complet en wrappant ``AgentCore.run()``.

    Args:
        prompt:      demande libre de l'utilisateur (requis, non vide) ;
        session_id:  session de conversation (mémoire short-term de l'agent) ;
        scope:       rôle agent sollicité, projeté sur ``Intent.role``
                     (cf. ``ia/agent/roles.py`` ; ne confondre ni avec le scope
                     de sécurité MCP ``MCPScopeRole`` — filtré par le serveur —
                     ni avec ``MCPSecurityScope``) ;
        core_factory: fabrique du noyau — ``None`` → ``build_agent_core()``
                     (LLM + registre réels). Injection dédiée aux tests.

    Returns:
        ``AgentRunResult`` — ``answer`` + ``actions`` (traces d'exécution) +
        budget consommé + éventuelle action en attente de validation humaine
        (``awaiting_action`` si ``status == PENDING_APPROVAL``).

    Raises:
        ValueError: ``prompt`` vide (le MCP tool surface en ``ToolError``).
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("orchestrate : 'prompt' requis (non vide).")
    factory = core_factory if core_factory is not None else _default_agent_core
    core = factory()
    return core.run(
        Intent(
            prompt=prompt,
            session_id=session_id or _DEFAULT_SESSION_ID,
            role=scope or _DEFAULT_SCOPE,
        )
    )


def _result_to_text(result: AgentRunResult) -> str:
    """Sérialise un ``AgentRunResult`` en texte JSON (réponse ``CallToolResult``).

    Molécule stable consommable par le client MCP : ``answer`` + traces
    (``actions``) + métadonnées de budget + marqueur ``awaiting_approval``
    (mutation soumise à validation humaine via ``decide_action()`` → ``APPROVE``).
    """
    payload: dict[str, Any] = {
        "answer": result.answer,
        "status": result.status.value,
        "rounds_used": result.rounds_used,
        "tool_calls_used": result.tool_calls_used,
        "actions": [action.model_dump(mode="json") for action in result.actions],
        "awaiting_approval": result.awaiting_action is not None,
    }
    if result.awaiting_action is not None:
        payload["awaiting_action"] = result.awaiting_action.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=False)


def build_orchestrate_tool(
    core_factory: Callable[[], AgentCore] | None = None,
    *,
    required_scope: MCPScopeRole = MCPScopeRole.CONTRIBUTOR,
) -> MCPTool:
    """Construit le tool MCP ``orchestrate`` (tool DISTINCT des tools bruts).

    Args:
        core_factory: fabrique du noyau injectée à ``orchestrate()`` — les
            tests branchent des fakes sans LLM/réseau ni SQLite ;
        required_scope: rôle minimal pour VOIR et APPELER le tool. CONTRIBUTOR
            par défaut : le tool peut déclencher des mutations (toutes passées
            en validation humaine par la policy), jamais exposé en read_only.

    Returns:
        Un ``MCPTool`` immuable, déclaré mutation (``MUTATING_ANNOTATIONS``),
        qui lève ``ToolError`` (réponse MCP ``isError: true``) sur un argument
        requis manquant ou un échec du run (le run lui-même ne lève jamais :
        ``AgentCore.run`` porte l'échec dans ``status``).
    """

    def handler(arguments: dict[str, Any]) -> str:
        args = dict(arguments or {})
        if "prompt" not in args or not str(args.get("prompt") or "").strip():
            raise ToolError(
                f"Argument(s) requis manquant(s) pour « {ORCHESTRATE_TOOL_NAME} » : prompt"
            )
        try:
            result = orchestrate(
                str(args["prompt"]),
                session_id=str(args.get("session_id") or _DEFAULT_SESSION_ID),
                scope=str(args.get("scope") or _DEFAULT_SCOPE),
                core_factory=core_factory,
            )
        except Exception as exc:  # échec de fabrication/validation → erreur métier
            raise ToolError(
                f"{ORCHESTRATE_TOOL_NAME} a échoué : {type(exc).__name__} : {exc}"
            ) from exc
        logger.info(
            "MCP orchestrate terminé : statut=%s actions=%d rounds=%d tools=%d",
            result.status.value, len(result.actions),
            result.rounds_used, result.tool_calls_used,
        )
        return _result_to_text(result)

    return MCPTool(
        name=ORCHESTRATE_TOOL_NAME,
        description=(
            "Orchestre l'agent ThinkTuning sur une demande libre : l'agent "
            "planifie, appelle ses outils internes et répond en synthétisant "
            "leurs résultats. Chaque action passe par la policy de sandbox — "
            "toute mutation (écriture/exécution) exige une validation humaine "
            "et met le run en attente (awaiting_approval). Retourne un JSON : "
            "{answer, status, actions, rounds_used, tool_calls_used, "
            "awaiting_approval}."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Demande libre de l'utilisateur (requis).",
                },
                "session_id": {
                    "type": "string",
                    "description": "Session de conversation (mémoire short-term).",
                },
                "scope": {
                    "type": "string",
                    "description": "Rôle agent sollicité (Intent.role).",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
        annotations=dict(MUTATING_ANNOTATIONS),
        required_scope=required_scope,
        handler=handler,
    )
