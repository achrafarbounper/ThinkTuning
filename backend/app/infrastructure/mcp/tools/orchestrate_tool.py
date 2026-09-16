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

import inspect
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from prometheus_client import Counter

from app.agent.core import AgentCore, AgentRunResult
from app.agent.policies.budget import BudgetPolicy
from app.domain.entities.mcp import MCPScopeRole, MCPTool
from app.domain.entities.plan import Intent
from app.domain.entities.run import RunStatus
from app.domain.ports import (
    ExecutionContext,
    MCPOrchestrationPort,
    MCPOrchestrationRequest,
    WorkerScopePolicy,
    normalize_mcp_event_granularity,
)
from app.infrastructure.mcp.manifest_generator import MUTATING_ANNOTATIONS
from app.infrastructure.mcp.mcp_events import (
    event_allowed_for_tool as _event_allowed_by_granularity,
)
from app.infrastructure.mcp.mcp_server import ToolError

# Politique d'événements : SOURCE UNIQUE ``app.infrastructure.mcp.mcp_events``
# (L2 — SCRUM-153). Ce module est PUR (aucune I/O) : l'import est sûr et ne
# peut pas créer de cycle avec le transport SSE. L'alias privé
# ``_event_allowed_by_granularity`` est conservé pour la rétro-compatibilité
# des appelants et tests historiques.

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
    """Gate MCP multi-agent : env explicite > base > env partagé > défaut.

    Résolution en cascade — la même valeur que celle utilisée par le reste du
    runtime pour ``flag_multi_agent`` (``VALEURS_PAR_DEFAUT`` /
    ``AGENT_MULTI_AGENT`` / ``AgentConfig.flag_multi_agent``), afin qu'une
    bascule du dashboard soit visible du transport MCP sans redémarrage :

    1. ``MCP_MULTI_AGENT_ENABLED`` — override LOCAL au transport MCP : dès
       qu'elle est définie, elle tranche (``0`` = repli mono-agent explicite,
       indépendamment de la configuration partagée) ;
    2. valeur PERSISTÉE en base (SQLite/Mongo, clé ``flag_multi_agent``) :
       seule une valeur explicitement écrite surclasse l'environnement
       partagé — même précédence que ``agent_settings.get_agent_settings`` ;
    3. ``AGENT_MULTI_AGENT`` — variable partagée du flag (défaut du module :
       activé, cf. ``FLAG_NAMES``) ;
    4. défaut du runtime (``VALEURS_PAR_DEFAUT``, flag activé) — jamais un
       fail-closed silencieux : une base vide ne doit pas transformer une
       activation produit en repli mono-agent (SCRUM-152).

    Toute erreur de lecture (base indisponible, mode Mongo sans client) est
    absorbée et retombe sur la couche suivante : le transport MCP n'échoue
    jamais à cause de la configuration.
    """
    local_override = os.getenv("MCP_MULTI_AGENT_ENABLED")
    if local_override is not None:
        return _as_bool(local_override, default=False)

    try:
        from app.infrastructure.persistence.agent_settings import (
            VALEURS_PAR_DEFAUT,
            get_settings_store,
        )

        persisted = get_settings_store().get_all()
        if "flag_multi_agent" in persisted:
            return _as_bool(persisted["flag_multi_agent"], default=False)
        default = _as_bool(VALEURS_PAR_DEFAUT.get("flag_multi_agent"), default=False)
    except Exception:
        default = False

    shared_env = os.getenv("AGENT_MULTI_AGENT")
    if shared_env is not None:
        return _as_bool(shared_env, default=False)

    return default


def _mcp_parallel_default() -> bool:
    """Parallélisme par défaut quand la requête ne précise pas ``parallel``.

    Miroir de ``AGENT_MULTI_PARALLEL`` (même clé que le cache du coordinateur,
    ``app/application/agent_cache.py``). L1 (SCRUM-152) : l'argument
    ``parallel`` d'une requête est désormais RÉELLEMENT transmis à
    l'orchestrateur ; l'absence d'argument conserve donc le défaut
    opérationnel au lieu de le désactiver silencieusement.
    """
    return _as_bool(os.getenv("AGENT_MULTI_PARALLEL"), default=False)


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
) -> ExecutionContext:
    """Valide (et retourne) le contexte RÉEL du worker.

    ``WorkerScopePolicy`` est appliquée au contexte ÉFFECTIF du worker (après
    ``ExecutionContext.for_worker``) et non au contexte parent : auparavant le
    contexte restreint était calculé puis jeté, si bien que la policy
    n'était jamais contraignante. Toute violation (outil interdit, plafond
    d'outils, scope élargi) lève ``ValueError`` → repli explicite
    ``worker_scope_violation`` côté transport.
    """
    parent = _build_execution_context(
        session_id=session_id,
        scope=scope,
        allowed_tools=("orchestrate", "read", "write"),
    )
    scope_policy = WorkerScopePolicy(
        parent_scope=(str(scope or _DEFAULT_SCOPE),),
        forbidden_tools=("system_shell",),
        max_tools_per_worker=4,
    )
    effective_tools = tuple(allowed_tools or ("orchestrate",))
    worker_context = parent.for_worker(
        worker_id,
        allowed_tools=effective_tools,
        allowed_resources=allowed_resources or ("session://default",),
        allowed_scopes=(str(scope or _DEFAULT_SCOPE),),
    )
    scope_policy.validate(worker_context, worker_id=worker_id)
    return worker_context


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


# ``_event_allowed_by_granularity`` est désormais l'alias importé de
# ``mcp_events.event_allowed_for_tool`` (voir l'import en tête de module) :
# la politique du chemin non-stream n'est plus dupliquée ici. Invariant commun
# aux deux transports — les événements terminaux portent la réponse finale et
# ne sont JAMAIS filtrés (garantie du terminal).


@dataclass(frozen=True)
class OrchestrationResolution:
    """Décision d'orchestration — contract partagé stream / non-stream.

    Le transport SSE (``mcp_server_sse._stream_orchestrate``) et le handler du
    tool ``orchestrate`` (``build_orchestrate_tool``) consomment CETTE décision :
    une requête identique ne peut donc pas produire un mode, un repli ou une
    granularité différents selon le transport employé.
    """

    mode: str
    requested_mode: str
    session_id: str
    scope: str
    model: str | None
    parallel: bool
    enable_thinking: bool
    event_granularity: str
    run_id: str | None = None
    resume_request_id: str | None = None
    task_id: str | None = None
    fallback: dict[str, Any] | None = None

    @property
    def is_multi_agent(self) -> bool:
        return self.mode == "multi_agent"

    @property
    def is_resuming(self) -> bool:
        """Reprise d'un run durable existant (identifiant durable fourni)."""
        return bool(self.run_id)

    def as_arguments(self) -> dict[str, Any]:
        """Arguments canoniques (traçabilité, tests de parité de décision)."""
        return {
            "mode": self.mode,
            "requested_mode": self.requested_mode,
            "session_id": self.session_id,
            "scope": self.scope,
            "model": self.model,
            "parallel": self.parallel,
            "enable_thinking": self.enable_thinking,
            "event_granularity": self.event_granularity,
            "run_id": self.run_id,
            "resume_request_id": self.resume_request_id,
            "task_id": self.task_id,
            "fallback": self.fallback,
        }


def resolve_orchestration(
    arguments: Mapping[str, Any] | None,
) -> OrchestrationResolution:
    """Résout la décision d'orchestration d'un appel MCP ``orchestrate``.

    SOURCE UNIQUE des décisions pour tous les transports (P0 — SCRUM-151) :

      1. validation de ``mode`` (``mono_agent`` | ``multi_agent``) ;
      2. garde ``MCP_MULTI_AGENT_ENABLED`` (override env explicite > valeur
         persistée > env partagé ``AGENT_MULTI_AGENT`` > défaut du runtime) —
         un mode multi-agent indisponible produit un repli EXPLICITE
         ``multi_agent_disabled`` (jamais un silence) ;
      3. ``WorkerScopePolicy`` sur le contexte EFFECTIF du worker → repli
         explicite ``worker_scope_violation`` ;
      4. normalisation de ``event_granularity`` (``ValueError`` → -32602 côté
         transport SSE, ``ToolError`` côté handler) ;
      5. découplage ``run_id`` (run durable reprenable) /
         ``resume_request_id`` (demande d'approbation AgentCore).

    Raises:
        ValueError: ``mode`` ou ``event_granularity`` invalide.
    """
    args = dict(arguments or {})
    requested_mode = str(args.get("mode") or "mono_agent").strip().lower() or "mono_agent"
    if requested_mode not in {"mono_agent", "multi_agent"}:
        raise ValueError("mode doit être « mono_agent » ou « multi_agent »")
    session_id = str(args.get("session_id") or _DEFAULT_SESSION_ID).strip() or _DEFAULT_SESSION_ID
    scope = str(args.get("scope") or _DEFAULT_SCOPE).strip() or _DEFAULT_SCOPE
    model = str(args["model"]) if args.get("model") else None
    parallel = (
        _as_bool(args.get("parallel"))
        if args.get("parallel") is not None
        else _mcp_parallel_default()
    )
    enable_thinking = _as_bool(args.get("enable_thinking"))
    event_granularity = normalize_mcp_event_granularity(args.get("event_granularity", "summary"))
    run_id = str(args["run_id"]).strip() if args.get("run_id") else None
    resume_request_id = (
        str(args["resume_request_id"]).strip() if args.get("resume_request_id") else None
    )
    task_id = str(args["task_id"]).strip() if args.get("task_id") else None

    mode = requested_mode
    fallback: dict[str, Any] | None = None
    if requested_mode == "multi_agent":
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
        else:
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
        if fallback is not None:
            mode = "mono_agent"
            logger.warning(
                "MCP orchestration repli mono-agent explicite : reason=%s details=%s",
                fallback.get("reason"),
                fallback.get("details"),
            )
            MCP_ORCHESTRATION_FALLBACK_TOTAL.labels(
                reason=str(fallback.get("reason") or "unknown"),
                mode="mono_agent",
            ).inc()

    return OrchestrationResolution(
        mode=mode,
        requested_mode=requested_mode,
        session_id=session_id,
        scope=scope,
        model=model,
        parallel=parallel,
        enable_thinking=enable_thinking,
        event_granularity=event_granularity,
        run_id=run_id,
        resume_request_id=resume_request_id,
        task_id=task_id,
        fallback=fallback,
    )


__all__ = [
    "MonoAgentOutcome",
    "ORCHESTRATE_TOOL_NAME",
    "OrchestrationResolution",
    "build_orchestrate_tool",
    "build_orchestration_request",
    "orchestrate",
    "orchestrate_multi_agent",
    "orchestrate_stream",
    "resolve_orchestration",
    "run_mono_agent",
]

# Identifiant MCP du tool — tranche AUSSI l'action d'audit dans
# ``MCPServer._audit_method`` (``ACT_MCP_ORCHESTRATE`` vs ``ACT_MCP_TOOL_CALL``).
ORCHESTRATE_TOOL_NAME = "orchestrate"

# Valeurs par défaut des arguments optionnels du tool (TOUJOURS sérialisables).
_DEFAULT_SESSION_ID = "default"
_DEFAULT_SCOPE = "default"


def _default_agent_core(
    *,
    enable_thinking: bool = False,
    approval_gateway: Callable[[Any], bool] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
) -> AgentCore:
    """Fabrique RÉELLE du noyau agentique (import paresseux, socle MCP léger).

    ``app.agent.factory.build_agent_core`` assemble le ``AgentCore`` complet
    (client LLM réel + registre legacy) depuis ``app/config/settings.py`` — il
    ne doit être importé qu'au moment de l'appel (jamais à l'import du module).

    ``approval_gateway`` : callback ``(Action) -> bool`` dérivé d'une demande
    d'approbation approuvée (``resume_request_id``). Sans gateway, TOUTE action
    ``APPROVE`` reste en attente — c'est le défaut fail-closed de la policy.
    """
    from app.agent.factory import build_agent_core

    return build_agent_core(
        approval_gateway=approval_gateway,
        enable_thinking=enable_thinking,
        on_thinking=on_thinking,
        on_tool_event=on_tool_event,
    )


def _mono_approval_gateway(
    resume_request_id: str | None,
    approval_store: Any | None = None,
) -> Callable[[Any], bool] | None:
    """Gateway d'approbation par EMPREINTE pour une reprise mono-agent.

    ``resume_request_id`` désigne une demande d'approbation AgentCore (table
    ``agent_approvals``) : seule l'action dont le fingerprint SHA-256 correspond
    à la demande approuvée est autorisée (aucune autre action ne passe).
    """
    if not resume_request_id:
        return None
    from app.application.run_lifecycle import make_approval_gateway, resolve_resume_hash

    store = approval_store
    if store is None:
        from app.infrastructure.persistence.approval_store import get_approval_store

        store = get_approval_store()
    resume_hash = resolve_resume_hash(store, resume_request_id)
    if not resume_hash:
        logger.warning(
            "MCP orchestrate : demande « %s » absente ou non approuvée — "
            "aucune action ne sera débloquée (fail-closed).",
            resume_request_id,
        )
    return make_approval_gateway(resume_hash)


def _persist_mono_approval(
    result: AgentRunResult,
    prompt: str,
    *,
    approval_store: Any | None = None,
) -> dict[str, Any] | None:
    """Persiste la demande d'approbation d'un run mono-agent en attente.

    Sans cette persistance, l'IHM recevait ``awaiting_approval: true`` SANS
    ``request_id`` : la carte de validation ne pouvait pas s'afficher et
    l'approbation HTTP restait impossible (P0 — SCRUM-151). Une panne du store
    ne casse jamais le run : le run conserve son statut ``pending_approval``.
    """
    if result.status is not RunStatus.PENDING_APPROVAL or result.awaiting_action is None:
        return None
    action = result.awaiting_action
    try:
        from app.application.run_lifecycle import create_approval_request

        store = approval_store
        if store is None:
            from app.infrastructure.persistence.approval_store import get_approval_store

            store = get_approval_store()
        payload = create_approval_request(store, action, prompt)
    except Exception as exc:  # pragma: no cover - panne de persistance
        logger.warning(
            "MCP orchestrate : impossible de persister la demande d'approbation (tool=%s) : %s",
            action.tool,
            exc,
        )
        return None
    logger.info(
        "MCP orchestrate en attente de validation humaine : request_id=%s tool=%s",
        payload.get("request_id"),
        action.tool,
    )
    return payload


@dataclass(frozen=True)
class MonoAgentOutcome:
    """Résultat mono-agent + demande d'approbation persistée (HITL MCP)."""

    result: AgentRunResult
    approval: dict[str, Any] | None = None

    @property
    def request_id(self) -> str | None:
        """Identifiant de la DEMANDE D'APPROBATION (jamais un identifiant de run)."""
        return (self.approval or {}).get("request_id")


def run_mono_agent(
    prompt: str,
    session_id: str = _DEFAULT_SESSION_ID,
    scope: str = _DEFAULT_SCOPE,
    *,
    resume_request_id: str | None = None,
    enable_thinking: bool = False,
    core_factory: Callable[..., AgentCore] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
    approval_store: Any | None = None,
) -> MonoAgentOutcome:
    """Exécute un run agentique mono-agent (noyau v2) avec gestion HITL.

    Args:
        prompt:      demande libre de l'utilisateur (requis, non vide) ;
        session_id:  session de conversation (mémoire short-term de l'agent) ;
        scope:       rôle agent sollicité, projeté sur ``Intent.role``
                     (cf. ``ia/agent/roles.py`` ; ne confondre ni avec le scope
                     de sécurité MCP ``MCPScopeRole`` — filtré par le serveur —
                     ni avec ``MCPSecurityScope``) ;
        resume_request_id: demande d'approbation APPROUVÉE à rejouer — seule
                     l'action dont l'empreinte SHA-256 correspond est débloquée ;
        core_factory: fabrique du noyau — ``None`` → ``build_agent_core()``
                     (LLM + registre réels). Injection dédiée aux tests.
        enable_thinking: active la collecte de la trace de réflexion du noyau.
        approval_store: store d'approbations injectable (tests) ; ``None`` →
                     store applicatif (MongoDB en production).

    Returns:
        ``MonoAgentOutcome`` — le ``AgentRunResult`` (``answer`` + ``actions`` +
        budget) ET le payload d'approbation ``{request_id, tool, args}`` lorsque
        le run s'arrête sur une mutation à valider (HITL).

    Raises:
        ValueError: ``prompt`` vide (le MCP tool surface en ``ToolError``).
    """
    budget_policy = BudgetPolicy.from_config()
    logger.debug("MCP orchestrate budget policy=%s", budget_policy.to_trace())
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("orchestrate : 'prompt' requis (non vide).")
    approval_gateway = _mono_approval_gateway(resume_request_id, approval_store)
    if core_factory is not None:
        # La fabrique reçoit la gateway de reprise quand sa signature l'accepte
        # (``**kwargs`` ou paramètre nommé ``approval_gateway``) — les fabriques
        # historiques zero-arg restent compatibles (tests).
        accepts_gateway = any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD or parameter.name == "approval_gateway"
            for parameter in inspect.signature(core_factory).parameters.values()
        )
        core = (
            core_factory(approval_gateway=approval_gateway) if accepts_gateway else core_factory()
        )
    else:
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
        core = _default_agent_core(
            enable_thinking=enable_thinking,
            approval_gateway=approval_gateway,
            on_thinking=on_thinking,
            on_tool_event=on_tool_event,
        )
    result = core.run(
        Intent(
            prompt=prompt,
            session_id=session_id or _DEFAULT_SESSION_ID,
            role=scope or _DEFAULT_SCOPE,
        )
    )
    return MonoAgentOutcome(
        result=result,
        approval=_persist_mono_approval(result, prompt, approval_store=approval_store),
    )


def orchestrate(
    prompt: str,
    session_id: str = _DEFAULT_SESSION_ID,
    scope: str = _DEFAULT_SCOPE,
    *,
    enable_thinking: bool = False,
    core_factory: Callable[[], AgentCore] | None = None,
    on_thinking: Callable[[str], None] | None = None,
    on_tool_event: Callable[[dict[str, Any]], None] | None = None,
    resume_request_id: str | None = None,
    approval_store: Any | None = None,
) -> AgentRunResult:
    """Wrapper historique : ``run_mono_agent(...).result`` (contrat inchangé).

    Les appelants qui ont besoin du ``request_id`` de la demande d'approbation
    (HITL MCP) utilisent ``run_mono_agent()``, qui retourne en plus le payload
    d'approbation persisté.
    """
    return run_mono_agent(
        prompt,
        session_id,
        scope,
        resume_request_id=resume_request_id,
        enable_thinking=enable_thinking,
        core_factory=core_factory,
        on_thinking=on_thinking,
        on_tool_event=on_tool_event,
        approval_store=approval_store,
    ).result


def orchestrate_stream(
    prompt: str,
    session_id: str = _DEFAULT_SESSION_ID,
    scope: str = _DEFAULT_SCOPE,
    *,
    enable_thinking: bool = False,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
    resume_request_id: str | None = None,
    core_factory: Callable[[], AgentCore] | None = None,
    approval_store: Any | None = None,
) -> MonoAgentOutcome:
    """Exécute ``run_mono_agent`` en exposant les événements de progression.

    Le run reste synchrone côté noyau, mais ses callbacks sont relayés au
    transport SSE. Le résultat retourné est un ``MonoAgentOutcome`` : le
    transport consomme ``.result`` (contrat MCP historique) ET ``.approval``
    (request_id de la demande d'approbation — découplé du run durable).
    """
    emit = on_event or (lambda _kind, _payload: None)
    return run_mono_agent(
        prompt,
        session_id=session_id,
        scope=scope,
        resume_request_id=resume_request_id,
        enable_thinking=enable_thinking,
        core_factory=core_factory,
        approval_store=approval_store,
        on_thinking=lambda chunk: emit("orchestrate.thinking", {"thinking_delta": chunk}),
        on_tool_event=lambda event: emit("orchestrate.tool", dict(event)),
    )


def build_orchestration_request(
    resolution: OrchestrationResolution,
    prompt: str,
) -> MCPOrchestrationRequest:
    """Construit la requête d'orchestration depuis une décision PARTAGÉE.

    SOURCE UNIQUE de construction (L1 — SCRUM-152) : le handler du tool et le
    transport SSE produisent exactement la même requête pour une même
    résolution — indispensable pour que la préparation durable du run
    (``prepare_run``, empreinte de requête) corresponde à l'exécution.
    """
    return MCPOrchestrationRequest.from_values(
        prompt=prompt,
        session_id=resolution.session_id or _DEFAULT_SESSION_ID,
        scope=resolution.scope or _DEFAULT_SCOPE,
        model=resolution.model,
        parallel=resolution.parallel,
        enable_thinking=resolution.enable_thinking,
        event_granularity=resolution.event_granularity,
        run_id=resolution.run_id,
        resume_request_id=resolution.resume_request_id,
        task_id=resolution.task_id,
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
    run_id: str | None = None,
    resume_request_id: str | None = None,
    task_id: str | None = None,
    orchestrator: MCPOrchestrationPort | None = None,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run the generic multi-agent coordinator through the MCP-specific port.

    ``run_id`` reprend un run durable EXISTANT (même identifiant) ;
    ``resume_request_id`` rejoue l'action approuvée de la sous-tâche bloquée ;
    ``task_id`` identifie cette sous-tâche (reprise ciblée, traçabilité). Ces
    trois valeurs sont indépendantes : un run durable peut être repris autant
    de fois que de validations humaines successives.
    """
    if on_event is None:
        from app.infrastructure.mcp.mcp_flow import current_recorder

        recorder = current_recorder()
        if recorder is not None:

            def record_event(kind: str, payload: dict[str, Any]) -> None:
                if not _event_allowed_by_granularity(kind, event_granularity):
                    return
                if kind in {"agent.worker.approval", "orchestrate.approval"}:
                    # L1 (SCRUM-152) : les approbations HITL sont tracées dans la
                    # Flow Map (nœud d'approbation), pas comme un simple outil.
                    raw = dict(payload or {})
                    approval = raw.get("approval")
                    detail = approval if isinstance(approval, dict) else raw
                    recorder.record_approval(
                        tool=str(detail.get("tool") or raw.get("tool") or ""),
                        message=str(raw.get("message") or ""),
                        request_id=raw.get("request_id"),
                    )
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
    request = build_orchestration_request(
        OrchestrationResolution(
            mode="multi_agent",
            requested_mode="multi_agent",
            session_id=session_id or _DEFAULT_SESSION_ID,
            scope=scope or _DEFAULT_SCOPE,
            model=model,
            parallel=parallel,
            enable_thinking=enable_thinking,
            event_granularity=event_granularity,
            run_id=run_id,
            resume_request_id=resume_request_id,
            task_id=task_id,
        ),
        prompt,
    )
    if orchestrator is None:
        from app.infrastructure.mcp.orchestration_factory import (
            build_mcp_orchestration_adapter,
        )

        orchestrator = build_mcp_orchestration_adapter()
    return orchestrator.run(request, on_event=on_event).model_dump(mode="json")


def _result_to_text(
    result: AgentRunResult,
    *,
    approval: dict[str, Any] | None = None,
    run_id: str | None = None,
    orchestration: dict[str, Any] | None = None,
) -> str:
    """Sérialise un ``AgentRunResult`` en texte JSON (réponse ``CallToolResult``).

    Molécule stable consommable par le client MCP : ``answer`` + traces
    (``actions``) + métadonnées de budget + marqueur ``awaiting_approval``
    (mutation soumise à validation humaine via ``decide_action()`` → ``APPROVE``).

    HITL (P0 — SCRUM-151) : ``request_id`` porte l'identifiant de la DEMANDE
    D'APPROBATION (table ``agent_approvals``) et ``run_id`` celui du run durable
    — deux valeurs délibérément distinctes. ``orchestration`` trace un éventuel
    repli mono-agent explicite (``orchestration_fallback``).
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
    if approval:
        payload["request_id"] = approval.get("request_id")
        payload["approval"] = approval
    if run_id:
        payload["run_id"] = run_id
    if orchestration:
        payload["orchestration"] = orchestration
    if result.thinking:
        payload["thinking"] = result.thinking
    return json.dumps(payload, ensure_ascii=False)


def build_orchestrate_tool(
    core_factory: Callable[[], AgentCore] | None = None,
    *,
    required_scope: MCPScopeRole = MCPScopeRole.CONTRIBUTOR,
    orchestrator: MCPOrchestrationPort | None = None,
    approval_store: Any | None = None,
) -> MCPTool:
    """Construit le tool MCP ``orchestrate`` (tool DISTINCT des tools bruts).

    Args:
        core_factory: fabrique du noyau injectée à ``orchestrate()`` — les
            tests branchent des fakes sans LLM/réseau ni SQLite ;
        required_scope: rôle minimal pour VOIR et APPELER le tool. CONTRIBUTOR
            par défaut : le tool peut déclencher des mutations (toutes passées
            en validation humaine par la policy), jamais exposé en read_only.
        approval_store: store d'approbations injectable (HITL) — ``None`` →
            store applicatif (MongoDB en production).

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
            # P0 (SCRUM-151) : décision d'orchestration MUTUALISÉE — le
            # transport SSE consomme exactement la même résolution
            # (mode, garde MCP_MULTI_AGENT_ENABLED, WorkerScopePolicy,
            # granularité, découplage run_id / resume_request_id).
            resolution = resolve_orchestration(args)
            if resolution.mode == "multi_agent":
                return json.dumps(
                    orchestrate_multi_agent(
                        str(args["prompt"]),
                        session_id=resolution.session_id,
                        scope=resolution.scope,
                        model=resolution.model,
                        parallel=resolution.parallel,
                        enable_thinking=resolution.enable_thinking,
                        event_granularity=resolution.event_granularity,
                        run_id=resolution.run_id,
                        resume_request_id=resolution.resume_request_id,
                        task_id=resolution.task_id,
                        orchestrator=orchestrator,
                    ),
                    ensure_ascii=False,
                )
            outcome = run_mono_agent(
                str(args["prompt"]),
                session_id=resolution.session_id,
                scope=resolution.scope,
                resume_request_id=resolution.resume_request_id,
                enable_thinking=resolution.enable_thinking,
                core_factory=core_factory,
                approval_store=approval_store,
            )
        except Exception as exc:  # échec de fabrication/validation → erreur métier
            raise ToolError(
                f"{ORCHESTRATE_TOOL_NAME} a échoué : {type(exc).__name__} : {exc}"
            ) from exc
        logger.info(
            "MCP orchestrate terminé : statut=%s actions=%d rounds=%d tools=%d",
            outcome.result.status.value,
            len(outcome.result.actions),
            outcome.result.rounds_used,
            outcome.result.tool_calls_used,
        )
        return _result_to_text(
            outcome.result,
            approval=outcome.approval,
            run_id=resolution.run_id,
            orchestration=resolution.fallback,
        )

    return MCPTool(
        name=ORCHESTRATE_TOOL_NAME,
        description=(
            "Orchestre l'agent ThinkTuning sur une demande libre : l'agent "
            "planifie, appelle ses outils internes et répond en synthétisant "
            "leurs résultats. Chaque action passe par la policy de sandbox — "
            "toute mutation (écriture/exécution) exige une validation humaine "
            "et met le run en attente (awaiting_approval). Retourne un JSON : "
            "{answer, status, actions, rounds_used, tool_calls_used, "
            "awaiting_approval, request_id, run_id}; le mode multi_agent ajoute "
            "plan, workers, synthesis, worker_errors et orchestration. "
            "request_id (demande d'approbation) et run_id (run durable) sont "
            "deux identifiants DISTINCTS : après validation, relancer avec "
            "resume_request_id=request_id ET run_id=run_id."
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
                    "description": (
                        "Identifiant d'une DEMANDE D'APPROBATION approuvée à "
                        "reprendre (autorise l'action correspondante, par "
                        "empreinte SHA-256)."
                    ),
                },
                "run_id": {
                    "type": "string",
                    "description": (
                        "Identifiant DURABLE d'un run existant à reprendre "
                        "(replay, reprise ciblée) — distinct de "
                        "resume_request_id."
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": (
                        "Sous-tâche (worker) à reprendre ciblée — purement "
                        "déclaratif, tracé pour l'audit et le Flow Map."
                    ),
                },
            },
            "required": ["prompt"],
            "additionalProperties": False,
        },
        annotations=dict(MUTATING_ANNOTATIONS),
        required_scope=required_scope,
        handler=handler,
    )
