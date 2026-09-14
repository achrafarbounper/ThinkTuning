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
          humaine suit le flux ``app/infrastructure/persistence/approval_store`` existant ;
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
import os
from collections.abc import Callable
from typing import Any

from prometheus_client import Counter

from app.agent.core import AgentCore, AgentRunResult
from app.agent.policies.budget import BudgetPolicy
from app.domain.entities.mcp import MCPScopeRole, MCPTool
from app.domain.entities.plan import Intent
from app.domain.ports import (
    ExecutionContext,
    MCPOrchestrationPort,
    MCPOrchestrationRequest,
    WorkerScopePolicy,
)
from app.infrastructure.mcp.manifest_generator import MUTATING_ANNOTATIONS
from app.infrastructure.mcp.mcp_server import ToolError

logger = logging.getLogger("thinktuning.mcp.orchestrate")

MCP_ORCHESTRATION_FALLBACK_TOTAL = Counter(
    "mcp_orchestration_fallback_total",
    "Nombre d'activation explicite du repli mono-agent sur l'orchestration MCP.",
    labelnames=("reason", "mode"),
)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _mcp_multi_agent_enabled() -> bool:
    """Gate MCP multi-agent : override env > config persistée > fail-closed.

    On conserve un repli sûr par défaut (mono-agent) pour rester compatible
    avec la surface historique, tout en acceptant une activation explicite via
    la configuration partagée du runtime.
    """
    for env_name in ("MCP_MULTI_AGENT_ENABLED", "AGENT_MULTI_AGENT"):
        raw = os.getenv(env_name)
        if raw is not None:
            return _as_bool(raw, default=False)

    try:
        from app.infrastructure.persistence.agent_settings import get_settings_store

        persisted = get_settings_store().get_all()
        if "flag_multi_agent" in persisted:
            return bool(persisted["flag_multi_agent"])
    except Exception:
        pass

    return False


def _build_execution_context(
    *,
    session_id: str,
    scope: str,
    allowed_tools: list[str] | tuple[str, ...] | None = None,
) -> ExecutionContext:
    """Creates a shared execution context for MCP/HTTP surfaces."""
    budget_policy = BudgetPolicy.from_config()
    allowed = tuple(allowed_tools or ("orchestrate", "read", "write"))
    scopes = (str(scope or _DEFAULT_SCOPE),)
    return ExecutionContext(
        user_id=str(session_id or _DEFAULT_SESSION_ID),
        tenant_id="default",
        allowed_tools=allowed,
        allowed_resources=("session://default",),
        allowed_scopes=scopes,
        budget=budget_policy,
        approvals=(),
    )


def _validate_worker_scope(
    *,
    session_id: str,
    scope: str,
    worker_id: str,
    allowed_tools: list[str] | tuple[str, ...],
    allowed_resources: list[str] | tuple[str, ...] | None = None,
) -> None:
    context = _build_execution_context(
        session_id=session_id,
        scope=scope,
        allowed_tools=("orchestrate",),
    )
    scope_policy = WorkerScopePolicy(
        parent_scope=(str(scope or _DEFAULT_SCOPE),),
        forbidden_tools=("system_shell",),
        max_tools_per_worker=4,
    )
    scope_policy.validate(context, worker_id=worker_id)
    context.for_worker(
        worker_id,
        allowed_tools=allowed_tools,
        allowed_resources=allowed_resources or ("session://default",),
        allowed_scopes=(str(scope or _DEFAULT_SCOPE),),
    )


def _fallback_payload(
    *,
    reason: str,
    mode: str = "mono_agent",
    details: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "mode": mode,
        "event": "orchestration_fallback",
        "fallback": "orchestration_fallback",
        "reason": reason,
    }
    if details is not None:
        payload["details"] = details
    if source is not None:
        payload["source"] = source
    return payload


def _normalize_agent_event(
    kind: str,
    payload: dict[str, Any] | None,
    *,
    parent_task_id: str | None = None,
    worker_id: str | None = None,
    phase: str | None = None,
) -> dict[str, Any]:
    event = dict(payload or {})
    event.setdefault("event", kind)
    if phase is not None:
        event.setdefault("phase", phase)
    elif kind.startswith("orchestrate.worker"):
        event.setdefault("phase", "worker")
    elif kind.startswith("orchestrate.synthesis"):
        event.setdefault("phase", "synthesis")
    else:
        event.setdefault("phase", "lead")
    event.setdefault("parent_task_id", parent_task_id)
    event.setdefault("worker_id", worker_id)
    return event


def _event_allowed_by_granularity(kind: str, granularity: str) -> bool:
    if granularity == "minimal":
        return kind in {
            "orchestrate.start",
            "orchestrate.done",
            "orchestrate.error",
            "orchestration_fallback",
        }
    if granularity == "summary":
        return kind in {
            "orchestrate.start",
            "orchestrate.worker",
            "orchestrate.synthesis",
            "orchestrate.done",
            "orchestrate.error",
            "orchestration_fallback",
        }
    return True


__all__ = [
    "ORCHESTRATE_TOOL_NAME",
    "build_orchestrate_tool",
    "orchestrate",
    "orchestrate_multi_agent",
    "orchestrate_stream",
]

# Identifiant MCP du tool — tranche AUSSI l'action d'audit dans
# ``MCPServer._audit_method`` (``ACT_MCP_ORCHESTRATE`` vs ``ACT_MCP_TOOL_CALL``).
ORCHESTRATE_TOOL_NAME = "orchestrate"

# Valeurs par défaut des arguments optionnels du tool (TOUJOURS sérialisables).
_DEFAULT_SESSION_ID = "default"
_DEFAULT_SCOPE = "default"


def _default_agent_core(*, enable_thinking: bool = False) -> AgentCore:
    """Fabrique RÉELLE du noyau agentique (import paresseux, socle MCP léger).

    ``app.agent.factory.build_agent_core`` assemble le ``AgentCore`` complet
    (client LLM réel + registre legacy) depuis ``app/config/settings.py`` — il
    ne doit être importé qu'au moment de l'appel (jamais à l'import du module).
    """
    from app.agent.factory import build_agent_core

    return build_agent_core(enable_thinking=enable_thinking)


def orchestrate(
    prompt: str,
    session_id: str = _DEFAULT_SESSION_ID,
    scope: str = _DEFAULT_SCOPE,
    *,
    enable_thinking: bool = False,
    core_factory: Callable[[], AgentCore] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
) -> AgentRunResult:
    budget_policy = BudgetPolicy.from_config()
    logger.debug("MCP orchestrate budget policy=%s", budget_policy.to_trace())
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
        enable_thinking: active la collecte de la trace de réflexion du noyau.

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
    if core_factory is not None:
        core = core_factory()
    else:
        from app.agent.factory import build_agent_core

        # Flow Map MCP (chemin non-streaming) : une session riche ouverte par
        # le transport (``MCPServer._handle_method`` → ``_CURRENT_RECORDER``)
        # est alimentée AUTOMATIQUEMENT — les callbacks explicites (SSE
        # streaming, qui gère lui-même le relais) restent prioritaires.
        if on_thinking is None or on_tool_event is None:
            from app.infrastructure.mcp.mcp_flow import current_recorder

            recorder = current_recorder()
            if recorder is not None:
                if on_thinking is None:
                    on_thinking = recorder.record_thinking
                if on_tool_event is None:
                    on_tool_event = recorder.record_tool
        core = build_agent_core(
            enable_thinking=enable_thinking,
            on_thinking=on_thinking,
            on_tool_event=on_tool_event,
        )
    return core.run(
        Intent(
            prompt=prompt,
            session_id=session_id or _DEFAULT_SESSION_ID,
            role=scope or _DEFAULT_SCOPE,
        )
    )


def orchestrate_stream(
    prompt: str,
    session_id: str = _DEFAULT_SESSION_ID,
    scope: str = _DEFAULT_SCOPE,
    *,
    enable_thinking: bool = False,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> AgentRunResult:
    """Exécute ``orchestrate`` en exposant les événements de progression.

    Le run reste synchrone côté noyau, mais ses callbacks sont relayés au
    transport SSE. Le résultat final conserve exactement le contrat MCP
    existant, ce qui permet au client de basculer progressivement.
    """
    emit = on_event or (lambda _kind, _payload: None)
    return orchestrate(
        prompt,
        session_id=session_id,
        scope=scope,
        enable_thinking=enable_thinking,
        on_thinking=lambda chunk: emit("orchestrate.thinking", {"thinking_delta": chunk}),
        on_tool_event=lambda event: emit("orchestrate.tool", dict(event)),
    )


def orchestrate_multi_agent(
    prompt: str,
    *,
    session_id: str = _DEFAULT_SESSION_ID,
    scope: str = _DEFAULT_SCOPE,
    model: str | None = None,
    parallel: bool = False,
    enable_thinking: bool = False,
    event_granularity: str = "summary",
    resume_request_id: str | None = None,
    orchestrator: MCPOrchestrationPort | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run the generic multi-agent coordinator through the MCP-specific port."""
    if on_event is None:
        from app.infrastructure.mcp.mcp_flow import current_recorder

        recorder = current_recorder()
        if recorder is not None:

            def record_event(kind: str, payload: dict[str, Any]) -> None:
                if not _event_allowed_by_granularity(kind, event_granularity):
                    return
                normalized = _normalize_agent_event(
                    kind,
                    dict(payload),
                    parent_task_id=str(session_id or _DEFAULT_SESSION_ID),
                    worker_id=str(payload.get("worker_id") or None) if payload else None,
                    phase=(
                        str(payload.get("phase") or "").strip().lower()
                        if payload and payload.get("phase") is not None
                        else None
                    ),
                )
                recorder.record_tool(normalized)

            on_event = record_event
    request = MCPOrchestrationRequest.from_values(
        prompt=prompt,
        session_id=session_id or _DEFAULT_SESSION_ID,
        scope=scope or _DEFAULT_SCOPE,
        model=model,
        parallel=parallel,
        enable_thinking=enable_thinking,
        event_granularity=event_granularity,
        resume_request_id=resume_request_id,
    )
    if orchestrator is None:
        from app.infrastructure.mcp.orchestration_factory import (
            build_mcp_orchestration_adapter,
        )

        orchestrator = build_mcp_orchestration_adapter()
    return orchestrator.run(request, on_event=on_event).model_dump(mode="json")


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
    if result.thinking:
        payload["thinking"] = result.thinking
    return json.dumps(payload, ensure_ascii=False)


def build_orchestrate_tool(
    core_factory: Callable[[], AgentCore] | None = None,
    *,
    required_scope: MCPScopeRole = MCPScopeRole.CONTRIBUTOR,
    orchestrator: MCPOrchestrationPort | None = None,
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
            mode = str(args.get("mode") or "mono_agent")
            if mode not in {"mono_agent", "multi_agent"}:
                raise ValueError("mode doit être « mono_agent » ou « multi_agent »")
            if mode == "multi_agent":
                session_id = str(args.get("session_id") or _DEFAULT_SESSION_ID)
                scope = str(args.get("scope") or _DEFAULT_SCOPE)
                if not _mcp_multi_agent_enabled():
                    source = (
                        "MCP_MULTI_AGENT_ENABLED"
                        if os.getenv("MCP_MULTI_AGENT_ENABLED") is not None
                        else (
                            "AGENT_MULTI_AGENT"
                            if os.getenv("AGENT_MULTI_AGENT") is not None
                            else "agent_config"
                        )
                    )
                    fallback = _fallback_payload(
                        reason="multi_agent_disabled",
                        mode="mono_agent",
                        source=source,
                    )
                    logger.warning(
                        "MCP multi-agent désactivé, fallback mono-agent; source=%s",
                        source,
                    )
                    MCP_ORCHESTRATION_FALLBACK_TOTAL.labels(
                        reason="multi_agent_disabled",
                        mode="mono_agent",
                    ).inc()
                    result = orchestrate(
                        str(args["prompt"]),
                        session_id=session_id,
                        scope=scope,
                        enable_thinking=bool(args.get("enable_thinking")),
                        core_factory=core_factory,
                    )
                    payload = json.loads(_result_to_text(result))
                    payload["orchestration"] = fallback
                    return json.dumps(payload, ensure_ascii=False)
                try:
                    _validate_worker_scope(
                        session_id=session_id,
                        scope=scope,
                        worker_id="planner",
                        allowed_tools=("orchestrate",),
                    )
                except ValueError as exc:
                    fallback = _fallback_payload(
                        reason="worker_scope_violation",
                        mode="mono_agent",
                        details=str(exc),
                        source="execution_context",
                    )
                    logger.warning("MCP multi-agent scope validation failed: %s", exc)
                    MCP_ORCHESTRATION_FALLBACK_TOTAL.labels(
                        reason="worker_scope_violation",
                        mode="mono_agent",
                    ).inc()
                    result = orchestrate(
                        str(args["prompt"]),
                        session_id=session_id,
                        scope=scope,
                        enable_thinking=bool(args.get("enable_thinking")),
                        core_factory=core_factory,
                    )
                    payload = json.loads(_result_to_text(result))
                    payload["orchestration"] = fallback
                    return json.dumps(payload, ensure_ascii=False)
                return json.dumps(
                    orchestrate_multi_agent(
                        str(args["prompt"]),
                        session_id=session_id,
                        scope=scope,
                        model=str(args["model"]) if args.get("model") else None,
                        parallel=bool(args.get("parallel")),
                        enable_thinking=bool(args.get("enable_thinking")),
                        event_granularity=str(args.get("event_granularity") or "summary"),
                        resume_request_id=(
                            str(args["resume_request_id"])
                            if args.get("resume_request_id")
                            else None
                        ),
                        orchestrator=orchestrator,
                    ),
                    ensure_ascii=False,
                )
            result = orchestrate(
                str(args["prompt"]),
                session_id=str(args.get("session_id") or _DEFAULT_SESSION_ID),
                scope=str(args.get("scope") or _DEFAULT_SCOPE),
                enable_thinking=bool(args.get("enable_thinking")),
                core_factory=core_factory,
            )
        except Exception as exc:  # échec de fabrication/validation → erreur métier
            raise ToolError(
                f"{ORCHESTRATE_TOOL_NAME} a échoué : {type(exc).__name__} : {exc}"
            ) from exc
        logger.info(
            "MCP orchestrate terminé : statut=%s actions=%d rounds=%d tools=%d",
            result.status.value,
            len(result.actions),
            result.rounds_used,
            result.tool_calls_used,
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
            "awaiting_approval}; le mode multi_agent ajoute plan, workers, "
            "synthesis, worker_errors et orchestration."
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
                "enable_thinking": {
                    "type": "boolean",
                    "description": "Active la trace de réflexion avant la réponse.",
                    "default": False,
                },
                "mode": {
                    "type": "string",
                    "enum": ["mono_agent", "multi_agent"],
                    "default": "mono_agent",
                },
                "model": {"type": "string"},
                "parallel": {"type": "boolean", "default": False},
                "event_granularity": {
                    "type": "string",
                    "enum": ["minimal", "summary", "verbose"],
                    "default": "summary",
                },
                "resume_request_id": {
                    "type": "string",
                    "description": "Identifiant d'une demande précédente à reprendre.",
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
        annotations=dict(MUTATING_ANNOTATIONS),
        required_scope=required_scope,
        handler=handler,
    )
