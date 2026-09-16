# project/app/infrastructure/mcp/mcp_metrics.py
"""Métriques MCP — observabilité de la résilience (L2 — SCRUM-153).

Trois familles, alignées sur les critères d'acceptation :

1. **Runs** : ``mcp_runs_total{status}`` (issue d'un run durable),
   ``mcp_runs_degraded_total{reason}`` (dégradations explicites) et
   ``mcp_runs_active`` (jauge des runs non terminaux) ;
2. **Concurrence** : ``mcp_sse_streams_active`` (jauge globale) et
   ``mcp_backpressure_rejections_total{scope}`` (rejets ``503``) ;
3. **Sécurité** : ``mcp_security_rejections_total{reason}`` (scope, quota,
   rate limit, policy, auth) — jamais de ``client_id`` en label (cardinalité
   non bornée) : la dimension client est portée par les logs d'audit.

Convention du projet : les compteurs sont créés au chargement du module et
enregistrés dans le ``REGISTRY`` par défaut, donc exposés par ``GET /metrics``
(même patron que ``app/api/middlewares/metrics.py`` et
``mcp/tools/orchestrate_tool.MCP_ORCHESTRATION_FALLBACK_TOTAL``).

Toute fonction ``record_*`` est DÉFENSIVE : l'observabilité ne doit jamais
faire échouer le transport (un label inattendu est normalisé, pas levé).
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge

# ---------------------------------------------------------------------------
# 1. Runs
# ---------------------------------------------------------------------------

#: Issues d'un run durable (``success`` | ``partial_success`` | ``failed`` |
#: ``awaiting_approval`` | ``in_progress``).
MCP_RUNS_TOTAL = Counter(
    "mcp_runs_total",
    "Runs durables MCP, par statut normalisé.",
    labelnames=("status",),
)

#: Dégradations explicites (``_meta.degraded``) — par cause canonique.
MCP_RUNS_DEGRADED_TOTAL = Counter(
    "mcp_runs_degraded_total",
    "Runs MCP ayant signalé une dégradation explicite (``_meta.degraded``).",
    labelnames=("reason",),
)

#: Runs non terminaux connus du store (jauge : alimentée par le sweeper).
MCP_RUNS_ACTIVE = Gauge(
    "mcp_runs_active",
    "Runs durables MCP non terminaux (pending/running/awaiting_approval).",
)

#: Runs réconciliés par le sweeper (``stale_run_reaped`` | ``lease_expired``).
MCP_RUNS_RECONCILED_TOTAL = Counter(
    "mcp_runs_reconciled_total",
    "Runs durables MCP réconciliés par le sweeper, par action.",
    labelnames=("action",),
)

# ---------------------------------------------------------------------------
# 2. Concurrence / backpressure
# ---------------------------------------------------------------------------

#: Flux SSE ``orchestrate`` actuellement ouverts (jauge globale).
MCP_SSE_STREAMS_ACTIVE = Gauge(
    "mcp_sse_streams_active",
    "Flux SSE orchestrate actuellement ouverts (global).",
)

#: Rejets de backpressure (``503`` + ``Retry-After``) par portée.
#: ``scope`` ∈ {global, client, quota}.
MCP_BACKPRESSURE_REJECTIONS_TOTAL = Counter(
    "mcp_backpressure_rejections_total",
    "Rejets MCP par saturation de capacité (503 + Retry-After).",
    labelnames=("scope",),
)

#: Flux SSE interrompus (déconnexion client) — distinction avec les échecs métier.
MCP_SSE_INTERRUPTED_TOTAL = Counter(
    "mcp_sse_interrupted_total",
    "Flux SSE MCP interrompus avant l'événement terminal.",
    labelnames=("reason",),
)

# ---------------------------------------------------------------------------
# 3. Sécurité / idempotence
# ---------------------------------------------------------------------------

#: Rejets de sécurité : ``scope`` | ``quota`` | ``rate_limit`` | ``policy`` | ``auth``.
MCP_SECURITY_REJECTIONS_TOTAL = Counter(
    "mcp_security_rejections_total",
    "Appels MCP rejetés par un garde-fou (scope, quota, débit, policy, auth).",
    labelnames=("reason",),
)

#: Requêtes portant ``Idempotency-Key`` : ``new`` | ``replay`` | ``conflict`` | ``inflight``.
MCP_IDEMPOTENCY_TOTAL = Counter(
    "mcp_idempotency_total",
    "Requêtes MCP avec Idempotency-Key, par issue.",
    labelnames=("outcome",),
)

#: Quotas d'ouverture de flux SSE (débit de ``POST /mcp/sse``).
MCP_SSE_QUOTA_REJECTIONS_TOTAL = Counter(
    "mcp_sse_quota_rejections_total",
    "Ouvertures de flux SSE refusées par quota de débit (429).",
    labelnames=("reason",),
)

_VALID_RUN_STATUSES = frozenset(
    {"success", "partial_success", "failed", "awaiting_approval", "in_progress"}
)
_VALID_BACKPRESSURE_SCOPES = frozenset({"global", "client", "quota"})
_VALID_SECURITY_REASONS = frozenset(
    {"scope", "quota", "rate_limit", "policy", "auth", "sse_quota"}
)
_VALID_IDEMPOTENCY_OUTCOMES = frozenset({"new", "replay", "conflict", "inflight"})
_VALID_SWEEPER_ACTIONS = frozenset({"stale_run_reaped", "lease_expired"})


def _bounded(value: str, allowed: frozenset[str], fallback: str) -> str:
    """Normalise un label (cardinalité bornée — jamais de valeur arbitraire)."""
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else fallback


def record_run_status(state: str) -> None:
    """Comptabilise l'issue d'un run durable (label borné)."""
    MCP_RUNS_TOTAL.labels(status=_bounded(state, _VALID_RUN_STATUSES, "in_progress")).inc()


def record_degradation(reason: str) -> None:
    """Comptabilise une dégradation explicite (``_meta.degraded``)."""
    MCP_RUNS_DEGRADED_TOTAL.labels(reason=str(reason or "unknown").strip() or "unknown").inc()


def set_active_runs(count: int) -> None:
    """Positionne la jauge des runs non terminaux."""
    MCP_RUNS_ACTIVE.set(max(0, int(count)))


def record_sweeper_action(action: str) -> None:
    """Comptabilise une réconciliation du sweeper."""
    MCP_RUNS_RECONCILED_TOTAL.labels(
        action=_bounded(action, _VALID_SWEEPER_ACTIONS, "stale_run_reaped")
    ).inc()


def record_backpressure(scope: str) -> None:
    """Comptabilise un rejet de backpressure (``global`` | ``client`` | ``quota``)."""
    MCP_BACKPRESSURE_REJECTIONS_TOTAL.labels(
        scope=_bounded(scope, _VALID_BACKPRESSURE_SCOPES, "global")
    ).inc()


def record_stream_interrupted(reason: str) -> None:
    """Comptabilise une interruption de flux SSE (sans événement terminal)."""
    MCP_SSE_INTERRUPTED_TOTAL.labels(
        reason=str(reason or "unknown").strip() or "unknown"
    ).inc()


def record_security_rejection(reason: str) -> None:
    """Comptabilise un rejet de sécurité (``scope`` | ``quota`` | ``rate_limit`` | ``policy``)."""
    MCP_SECURITY_REJECTIONS_TOTAL.labels(
        reason=_bounded(reason, _VALID_SECURITY_REASONS, "scope")
    ).inc()


def record_idempotency(outcome: str) -> None:
    """Comptabilise l'issue d'une requête idempotente."""
    MCP_IDEMPOTENCY_TOTAL.labels(
        outcome=_bounded(outcome, _VALID_IDEMPOTENCY_OUTCOMES, "new")
    ).inc()


def record_sse_quota_rejection(reason: str) -> None:
    """Comptabilise un refus par quota de flux SSE."""
    MCP_SSE_QUOTA_REJECTIONS_TOTAL.labels(reason=str(reason or "rate").strip() or "rate").inc()


__all__ = [
    "MCP_BACKPRESSURE_REJECTIONS_TOTAL",
    "MCP_IDEMPOTENCY_TOTAL",
    "MCP_RUNS_ACTIVE",
    "MCP_RUNS_DEGRADED_TOTAL",
    "MCP_RUNS_RECONCILED_TOTAL",
    "MCP_RUNS_TOTAL",
    "MCP_SECURITY_REJECTIONS_TOTAL",
    "MCP_SSE_INTERRUPTED_TOTAL",
    "MCP_SSE_QUOTA_REJECTIONS_TOTAL",
    "MCP_SSE_STREAMS_ACTIVE",
    "record_backpressure",
    "record_degradation",
    "record_idempotency",
    "record_run_status",
    "record_security_rejection",
    "record_sse_quota_rejection",
    "record_stream_interrupted",
    "record_sweeper_action",
    "set_active_runs",
]
