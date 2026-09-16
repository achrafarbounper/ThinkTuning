# project/app/infrastructure/mcp/mcp_events.py
"""Politique d'événements MCP : SOURCE UNIQUE DE VÉRITÉ (L2 — SCRUM-153).

Avant L2, la politique de filtrage des événements était DUPLIQUÉE :

* transport SSE  : ``mcp_server_sse._TERMINAL_SSE_KINDS`` +
  ``mcp_server_sse._event_allowed_for_sse`` ;
* tool orchestrate : ``orchestrate_tool._event_allowed_by_granularity``.

Les deux jeux divergeaient sans raison métier (le défaut SSE est une whitelist,
le défaut du tool est un passe-plat) : un même run pouvait donc être filtré
différemment selon le transport, et l'ajout d'un nouvel événement se faisait à
deux endroits. Ce module centralise les invariants et expose deux projections
dont le comportement est PRÉSERVÉ À L'IDENTIQUE (anti-régression stricte).

Invariants (docs/mcp/MULTI_AGENT_SSE_FLOW.md §2/§5) :

1. **Terminal** (``orchestrate.done`` / ``orchestrate.error`` / ``message`` /
   ``agent.done`` / ``agent.error``) : JAMAIS filtré, quelle que soit la
   granularité — c'est la garantie de la réponse finale ;
2. ``verbose`` : tout passe (le client a explicitement demandé la trace
   complète) ;
3. ``minimal`` : seuls les jalons (start / done / error / repli explicite) ;
4. ``summary`` : jalons + workers + synthèse ;
5. Défaut SSE : whitelist stricte (projection du modèle multi-agent) —
   historique conservé tel quel ;
6. Défaut tool : passe-plat (le tool est déjà filtré côté transport).

Ce module est PUR (aucune I/O, aucun état) : il est importable sans cycle ni
dépendance lourde, et directement testable.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# ---------------------------------------------------------------------------
# Granularités (miroir de ``app.domain.ports.mcp_ports.VALID_MCP_EVENT_GRANULARITIES``)
# ---------------------------------------------------------------------------

GRANULARITY_MINIMAL = "minimal"
GRANULARITY_SUMMARY = "summary"
GRANULARITY_VERBOSE = "verbose"

VALID_EVENT_GRANULARITIES: frozenset[str] = frozenset(
    {GRANULARITY_MINIMAL, GRANULARITY_SUMMARY, GRANULARITY_VERBOSE}
)

# ---------------------------------------------------------------------------
# Noms d'événements (source unique — plus aucune chaîne magique dispersée)
# ---------------------------------------------------------------------------

EVENT_STARTED = "orchestrate.started"
EVENT_START = "orchestrate.start"
EVENT_THINKING = "orchestrate.thinking"
EVENT_TOOL = "orchestrate.tool"
EVENT_WORKER = "orchestrate.worker"
EVENT_SYNTHESIS = "orchestrate.synthesis"
EVENT_SYNTHESIZING = "orchestrate.synthesizing"
EVENT_DONE = "orchestrate.done"
EVENT_ERROR = "orchestrate.error"
EVENT_MESSAGE = "message"
EVENT_FALLBACK = "orchestration_fallback"

# Reprise / replay durable (L1 — SCRUM-152, consommés par le transport SSE)
EVENT_REPLAY_STARTED = "replay_started"
EVENT_REPLAY_EVENT = "orchestrate.replay"
EVENT_REPLAY_COMPLETED = "replay_completed"
EVENT_REPLAY_ERROR = "replay.error"

# Résilience (L2 — SCRUM-153) : signaux explicites de dégradation / garde-fou.
EVENT_DEGRADED = "orchestrate.degraded"

# Progression SSE bas niveau (relais du flux core) : jamais filtrés par la
# granularité — ils portent le temps réel demandé par le client.
CORE_PROGRESS_EVENTS: frozenset[str] = frozenset(
    {EVENT_THINKING, EVENT_TOOL, EVENT_WORKER, EVENT_SYNTHESIS, EVENT_SYNTHESIZING}
)

#: Événements terminaux : ils portent la réponse finale et ne sont JAMAIS
#: filtrés ni abandonnés (ni granularité, ni déconnexion transitoire).
TERMINAL_EVENT_KINDS: frozenset[str] = frozenset(
    {EVENT_DONE, EVENT_ERROR, EVENT_MESSAGE, "agent.done", "agent.error"}
)

#: Sentinelle de fin de flux SSE (contrat front : sortie sur sentinelle).
SSE_END_SENTINEL = "data: [DONE]\n\n"

#: Commentaire de garde émis pendant l'attente du worker (anti-timeout proxy).
SSE_HEARTBEAT = ": heartbeat\n\n"

# ---------------------------------------------------------------------------
# Projection SSE (transport) — comportement HISTORIQUE préservé à l'identique
# ---------------------------------------------------------------------------

#: ``minimal`` côté transport : jalons stricts (l'entité ``started`` est incluse
#: depuis L1 : le client doit connaître son ``run_id`` pour reprendre).
SSE_MINIMAL_EVENT_KINDS: frozenset[str] = frozenset(
    {EVENT_STARTED, EVENT_START, EVENT_DONE, EVENT_ERROR, EVENT_MESSAGE}
)

#: Défaut SSE (``summary``) : whitelist du modèle multi-agent.
SSE_DEFAULT_EVENT_KINDS: frozenset[str] = frozenset(
    {
        EVENT_STARTED,
        EVENT_START,
        EVENT_THINKING,
        EVENT_TOOL,
        EVENT_WORKER,
        EVENT_SYNTHESIS,
        EVENT_SYNTHESIZING,
        EVENT_DONE,
        EVENT_ERROR,
        EVENT_MESSAGE,
        EVENT_FALLBACK,
        EVENT_DEGRADED,
        # Legacy coordinator event names remain the source of truth for the
        # multi-agent adapter and must not be dropped by the MCP projection.
        "agent.plan",
        "agent.resuming",
        "agent.worker.start",
        "agent.worker.tool",
        "agent.worker.thinking",
        "agent.worker.result",
        "agent.worker.error",
        "agent.worker.approval",
        "agent.phase",
        "agent.synthesizing",
        "agent.done",
        "agent.error",
    }
)

#: Familles d'événements dynamiques acceptées en défaut SSE.
SSE_DEFAULT_EVENT_PREFIXES: tuple[str, ...] = (
    "orchestrate.worker.",
    "orchestrate.synthesis.",
)

# ---------------------------------------------------------------------------
# Projection tool (``orchestrate`` — chemin non-stream)
# ---------------------------------------------------------------------------

TOOL_MINIMAL_EVENT_KINDS: frozenset[str] = frozenset(
    {EVENT_START, EVENT_DONE, EVENT_ERROR, EVENT_FALLBACK}
)

TOOL_SUMMARY_EVENT_KINDS: frozenset[str] = frozenset(
    {EVENT_START, EVENT_WORKER, EVENT_SYNTHESIS, EVENT_DONE, EVENT_ERROR, EVENT_FALLBACK}
)

#: Projections disponibles : ``sse`` (transport) / ``tool`` (chemin non-stream).
PROJECTION_SSE = "sse"
PROJECTION_TOOL = "tool"


def normalize_granularity(value: Any, *, default: str = GRANULARITY_SUMMARY) -> str:
    """Canonicalise une granularité SANS lever (fail-safe par défaut).

    Le transport reçoit déjà une valeur validée par
    ``normalize_mcp_event_granularity`` ; cette fonction tolère donc toute
    entrée (``None``, casse, espace) et retombe sur ``summary`` — jamais
    d'exception dans un chemin de streaming.
    """
    normalized = str(value or "").strip().lower()
    return normalized if normalized in VALID_EVENT_GRANULARITIES else default


def is_terminal_event(kind: str) -> bool:
    """``True`` si l'événement porte la réponse finale (jamais filtré)."""
    return str(kind or "") in TERMINAL_EVENT_KINDS


def event_allowed(kind: str, granularity: str, *, projection: str = PROJECTION_SSE) -> bool:
    """Décision de filtrage UNIQUE (invariant 1 : terminal toujours autorisé).

    Args:
        kind:       nom d'événement (``orchestrate.thinking``, …) ;
        granularity: ``minimal`` | ``summary`` | ``verbose`` (tolérant) ;
        projection: ``sse`` (défaut whitelist) | ``tool`` (défaut passe-plat).
    """
    if is_terminal_event(kind):
        return True
    level = normalize_granularity(granularity)
    if projection == PROJECTION_TOOL:
        if level == GRANULARITY_MINIMAL:
            return kind in TOOL_MINIMAL_EVENT_KINDS
        if level == GRANULARITY_SUMMARY:
            return kind in TOOL_SUMMARY_EVENT_KINDS
        return True
    if level == GRANULARITY_VERBOSE:
        return True
    if level == GRANULARITY_MINIMAL:
        return kind in SSE_MINIMAL_EVENT_KINDS
    return kind in SSE_DEFAULT_EVENT_KINDS or kind.startswith(SSE_DEFAULT_EVENT_PREFIXES)


def event_allowed_for_sse(kind: str, granularity: str) -> bool:
    """Projection transport SSE (comportement historique, centralisé ici).

    Les événements terminaux bypassent TOUJOURS le filtre : ils portent la
    réponse finale et ne doivent jamais être abandonnés (invariant 2/3 de
    ``docs/mcp/MULTI_AGENT_SSE_FLOW.md``).
    """
    return event_allowed(kind, granularity, projection=PROJECTION_SSE)


def event_allowed_for_tool(kind: str, granularity: str) -> bool:
    """Projection du chemin non-stream (``orchestrate`` tool)."""
    return event_allowed(kind, granularity, projection=PROJECTION_TOOL)


# ---------------------------------------------------------------------------
# Dégradation explicite — ``result._meta`` (invariant AC L2)
# ---------------------------------------------------------------------------

#: Causes de dégradation (vocabulaire stable, exploité par l'UI et les métriques).
DEGRADATION_MULTI_AGENT_FALLBACK = "multi_agent_fallback"
DEGRADATION_CLIENT_DISCONNECTED = "client_disconnected"
DEGRADATION_SYNTHESIS_ERROR = "synthesis_error"
DEGRADATION_STALE_RUN = "stale_run_reaped"
DEGRADATION_LEASE_EXPIRED = "lease_expired"
DEGRADATION_BACKPRESSURE = "backpressure"
DEGRADATION_BUDGET = "budget_exceeded"

VALID_DEGRADATION_REASONS: frozenset[str] = frozenset(
    {
        DEGRADATION_MULTI_AGENT_FALLBACK,
        DEGRADATION_CLIENT_DISCONNECTED,
        DEGRADATION_SYNTHESIS_ERROR,
        DEGRADATION_STALE_RUN,
        DEGRADATION_LEASE_EXPIRED,
        DEGRADATION_BACKPRESSURE,
        DEGRADATION_BUDGET,
    }
)

#: Phases de défaillance normalisées (miroir de ``VALID_MCP_FAILURE_PHASES``).
VALID_FAILURE_PHASES: frozenset[str] = frozenset({"lead", "worker", "synthesis"})


def build_meta(
    *,
    run_id: str | None = None,
    degraded: bool = False,
    reason: str | None = None,
    failure_phase: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Construit le bloc ``_meta`` D'UN résultat MCP (dégradation explicite).

    Le contrat L2 impose que toute dégradation soit lisible par le client sans
    deviner : ``_meta.degraded`` est TOUJOURS présent (``False`` = nominal),
    accompagné de ``run_id`` (traçabilité/reprise) et ``failure_phase``
    (``lead`` | ``worker`` | ``synthesis`` | ``None``).

    Args:
        run_id:        identifiant du run durable (``None`` en mono-agent) ;
        degraded:      ``True`` dès qu'une garantie a été relâchée ;
        reason:        cause canonique (``multi_agent_fallback``, …) ;
        failure_phase: phase fautive normalisée (``None`` si succès) ;
        extra:         champs additionnels (jamais prioritaires sur le contrat).

    Returns:
        Bloc ``_meta`` sérialisable JSON, champs de contrat garantis présents.
    """
    normalized_reason = str(reason or "").strip() or None
    normalized_phase = str(failure_phase or "").strip().lower() or None
    if normalized_phase is not None and normalized_phase not in VALID_FAILURE_PHASES:
        raise ValueError(f"failure_phase must be one of {sorted(VALID_FAILURE_PHASES)}")
    meta: dict[str, Any] = {
        "degraded": bool(degraded) or normalized_reason is not None,
        "run_id": str(run_id) if run_id else None,
        "failure_phase": normalized_phase,
        "reason": normalized_reason,
    }
    if extra:
        for key, value in extra.items():
            meta.setdefault(str(key), value)
    return meta


def degraded_meta(
    reason: str,
    *,
    run_id: str | None = None,
    failure_phase: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Raccourci ``build_meta(degraded=True, …)`` — lecture intentionnelle."""
    return build_meta(
        run_id=run_id,
        degraded=True,
        reason=reason,
        failure_phase=failure_phase,
        extra=extra,
    )


#: Libellés de statut normalisés d'un run (miroir de l'entité durable).
RUN_STATUS_SUCCESS = "success"
RUN_STATUS_PARTIAL_SUCCESS = "partial_success"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_AWAITING_APPROVAL = "awaiting_approval"
RUN_STATUS_IN_PROGRESS = "in_progress"

#: Vocabulaire de ``domain.entities.run.RunStatus`` (statut terminal du noyau)
#: projeté sur les libellés bornés — ``budget_exhausted`` / ``rejected_loop``
#: sont des ABOUTISSEMENTS sans réponse finale (garantie relâchée) : ils sont
#: donc comptés comme échecs, jamais comme « en cours » (sinon la jauge des runs
#: actifs serait alimentée par un compteur d'échecs).
_RUN_STATUS_BY_CORE_STATUS: dict[str, str] = {
    "completed": RUN_STATUS_SUCCESS,
    "success": RUN_STATUS_SUCCESS,
    "succeeded": RUN_STATUS_SUCCESS,
    "ok": RUN_STATUS_SUCCESS,
    "partial_success": RUN_STATUS_PARTIAL_SUCCESS,
    "partial": RUN_STATUS_PARTIAL_SUCCESS,
    "failed": RUN_STATUS_FAILED,
    "error": RUN_STATUS_FAILED,
    "cancelled": RUN_STATUS_FAILED,
    "cancelling": RUN_STATUS_FAILED,
    "interrupted": RUN_STATUS_FAILED,
    "budget_exhausted": RUN_STATUS_FAILED,
    "rejected_loop": RUN_STATUS_FAILED,
    "awaiting_approval": RUN_STATUS_AWAITING_APPROVAL,
    "pending_approval": RUN_STATUS_AWAITING_APPROVAL,
}


def status_to_run_metric_label(state: str) -> str:
    """Projette un état (FSM durable OU noyau) en label de métrique borné.

    Deux vocabulaires cohabitent dans le projet (``MCPDurableRunState.state`` :
    ``completed``/``partial_success``/``failed``/``cancelled`` ; ``RunStatus``
    du noyau : ``completed``/``pending_approval``/``budget_exhausted``/
    ``rejected_loop``/``failed``) : la projection est TOTALE sur les deux et
    retombe sur ``in_progress`` pour tout état non terminal inconnu
    (cardinalité bornée garantie).
    """
    normalized = str(state or "").strip().lower()
    return _RUN_STATUS_BY_CORE_STATUS.get(normalized, RUN_STATUS_IN_PROGRESS)


__all__ = [
    "CORE_PROGRESS_EVENTS",
    "DEGRADATION_BACKPRESSURE",
    "DEGRADATION_BUDGET",
    "DEGRADATION_CLIENT_DISCONNECTED",
    "DEGRADATION_LEASE_EXPIRED",
    "DEGRADATION_MULTI_AGENT_FALLBACK",
    "DEGRADATION_STALE_RUN",
    "DEGRADATION_SYNTHESIS_ERROR",
    "EVENT_DEGRADED",
    "EVENT_DONE",
    "EVENT_ERROR",
    "EVENT_FALLBACK",
    "EVENT_MESSAGE",
    "EVENT_REPLAY_COMPLETED",
    "EVENT_REPLAY_ERROR",
    "EVENT_REPLAY_EVENT",
    "EVENT_REPLAY_STARTED",
    "EVENT_START",
    "EVENT_STARTED",
    "EVENT_SYNTHESIS",
    "EVENT_SYNTHESIZING",
    "EVENT_THINKING",
    "EVENT_TOOL",
    "EVENT_WORKER",
    "GRANULARITY_MINIMAL",
    "GRANULARITY_SUMMARY",
    "GRANULARITY_VERBOSE",
    "PROJECTION_SSE",
    "PROJECTION_TOOL",
    "RUN_STATUS_AWAITING_APPROVAL",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_IN_PROGRESS",
    "RUN_STATUS_PARTIAL_SUCCESS",
    "RUN_STATUS_SUCCESS",
    "SSE_DEFAULT_EVENT_KINDS",
    "SSE_DEFAULT_EVENT_PREFIXES",
    "SSE_END_SENTINEL",
    "SSE_HEARTBEAT",
    "SSE_MINIMAL_EVENT_KINDS",
    "TERMINAL_EVENT_KINDS",
    "TOOL_MINIMAL_EVENT_KINDS",
    "TOOL_SUMMARY_EVENT_KINDS",
    "VALID_DEGRADATION_REASONS",
    "VALID_EVENT_GRANULARITIES",
    "VALID_FAILURE_PHASES",
    "build_meta",
    "degraded_meta",
    "event_allowed",
    "event_allowed_for_sse",
    "event_allowed_for_tool",
    "is_terminal_event",
    "normalize_granularity",
    "status_to_run_metric_label",
]
