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
   non bornée) : la dimension client est portée par les logs d'audit ;
4. **Observabilité 2.3.0 (SCRUM-161)** : ``mcp_tool_calls_total{tool}`` /
   ``mcp_tool_errors_total{tool}`` (volume et erreurs PAR TOOL),
   ``mcp_request_latency_seconds{method}`` (histogramme → p50/p95/p99 via
   ``histogram_quantile``, + ``latency_quantiles()`` in-process pour les
   surfaces sans PromQL), ``mcp_sessions_active`` (jauge alimentée par
   ``SessionTracker``), ``mcp_runs_awaiting_approval`` (HITL, alimentée par le
   sweeper), ``mcp_sse_reconnections_total{mode}`` (mode de curseur de reprise)
   et ``mcp_rate_limit_rejections_total`` (rejets de débit MCP).

Convention du projet : les compteurs sont créés au chargement du module et
enregistrés dans le ``REGISTRY`` par défaut, donc exposés par ``GET /metrics``
(même patron que ``app/api/middlewares/metrics.py`` et
``mcp/tools/orchestrate_tool.MCP_ORCHESTRATION_FALLBACK_TOTAL``).

Toute fonction ``record_*`` est DÉFENSIVE : l'observabilité ne doit jamais
faire échouer le transport (un label inattendu est normalisé, pas levé).
"""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque

from prometheus_client import Counter, Gauge, Histogram

from app.infrastructure.mcp.protocol import MCPMethod

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

# ---------------------------------------------------------------------------
# 4. Observabilité MCP 2.3.0 — volume, latence, HITL, sessions, reconnexions
# ---------------------------------------------------------------------------

#: Volume d'appels ``tools/call`` PAR TOOL (cardinalité bornée : un tool
#: inconnu retombe sur ``unknown`` — jamais de série parasite).
MCP_TOOL_CALLS_TOTAL = Counter(
    "mcp_tool_calls_total",
    "Appels tools/call MCP par tool (volume).",
    labelnames=("tool",),
)

#: Erreurs PAR TOOL (``isError`` ou exception interne) — alimente le taux
#: d'erreur par tool (ratio avec ``mcp_tool_calls_total{tool}``).
MCP_TOOL_ERRORS_TOTAL = Counter(
    "mcp_tool_errors_total",
    "Appels tools/call MCP en échec, par tool.",
    labelnames=("tool",),
)

#: Latence des requêtes MCP (par méthode) — Histogramme à BUCKETS explicites :
#: ``p50`` / ``p95`` / ``p99`` sont calculés par PromQL
#: (``histogram_quantile(0.95, sum by (le) (rate(..._bucket[5m])))``) — la
#: version de ``prometheus_client`` du projet ne calcule plus de quantiles
#: in-process dans ``Summary`` (``Summary.__init__`` sans ``objectives``), et
#: un histogramme est de toute façon AGRÉGEABLE entre instances, contrairement
#: à des quantiles par processus. Les mêmes quantiles sont exposés EN CLAIR
#: (fenêtre glissante, ``LatencyWindow``) par ``/api/v1/mcp/metrics`` pour les
#: surfaces sans PromQL (dashboard interne).
#: Buckets : 5 ms → 300 s (couvre le catalogue ET les runs LLM de plusieurs
#: minutes) + ``+Inf`` implicite.
LATENCY_BUCKETS_SECONDS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
)

MCP_REQUEST_LATENCY = Histogram(
    "mcp_request_latency_seconds",
    "Latence des requêtes MCP par méthode (quantiles via histogram_quantile).",
    labelnames=("method",),
    buckets=LATENCY_BUCKETS_SECONDS,
)

#: Sessions MCP actives (jauge : incrémentée/décrémentée par le transport SSE
#: via ``SessionTracker`` — éviction TTL des sessions fantômes).
MCP_SESSIONS_ACTIVE = Gauge(
    "mcp_sessions_active",
    "Sessions MCP actives (flux SSE ouverts).",
)

#: Runs en attente d'approbation HUMAINE (HITL) — jauge alimentée par le
#: sweeper (même source de vérité que ``mcp_runs_active``).
MCP_RUNS_AWAITING_APPROVAL = Gauge(
    "mcp_runs_awaiting_approval",
    "Runs durables MCP en attente d'approbation humaine (HITL).",
)

#: Reconnexions de flux SSE — ``resume_token`` | ``last_event_id`` |
#: ``after_sequence`` : le mode de reprise renseigne l'état du client.
MCP_SSE_RECONNECTIONS_TOTAL = Counter(
    "mcp_sse_reconnections_total",
    "Reconnexions de flux SSE MCP, par mode de reprise.",
    labelnames=("mode",),
)

#: Rejets par rate limit per-client (compteur DÉDIÉ — critère 2.3.0).
MCP_RATE_LIMIT_REJECTIONS_TOTAL = Counter(
    "mcp_rate_limit_rejections_total",
    "Appels MCP rejetés par rate limit per-client (429).",
)

_VALID_RECONNECTION_MODES = frozenset({"resume_token", "last_event_id", "after_sequence"})

#: Forme normalisée d'un label de tool : minuscules, ``[a-z0-9_.:-]``, 64
#: caractères max. Le serveur REJETTE déjà tout nom hors catalogue (« Unknown
#: tool » — ``mcp_server._handle_tools_call``) : la cardinalité reste donc
#: celle du registre de tools ; la normalisation, elle, garantit qu'aucune
#: valeur arbitraire (espaces, contrôle, très longue) ne crée de série.
_TOOL_LABEL_MAX_LENGTH = 64
_TOOL_LABEL_INVALID_CHARS = re.compile(r"[^a-z0-9_.:-]")

#: Méthodes acceptées comme label de latence (cardinalité BORNÉE) : les
#: méthodes du protocole (``MCPMethod``, source unique) + ``unknown`` pour tout
#: identifiant hors catalogue — un client ne peut PAS créer de série arbitraire.
_ALLOWED_METHOD_LABELS = frozenset(
    value
    for name, value in vars(MCPMethod).items()
    if not name.startswith("_") and isinstance(value, str)
) | {"unknown"}

#: Bornes de la fenêtre glissante des quantiles (mémoire + cardinalité).
_LATENCY_WINDOW_SIZE = 512
_LATENCY_MAX_METHODS = 32


# ---------------------------------------------------------------------------
# Fenêtre glissante de latence (quantiles p50/p95/p99 sans PromQL)
# ---------------------------------------------------------------------------


def _quantile(values: list[float], fraction: float) -> float:
    """Quantile par interpolation linéaire (aucune dépendance externe).

    Args:
        values: échantillons TRIÉS (croissants) ;
        fraction: position demandée dans ``[0, 1]`` (0.95 = p95).

    Returns:
        Quantile interpolé (``0.0`` sur une liste vide).
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    position = fraction * (len(values) - 1)
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return values[low]
    weight = position - low
    return values[low] * (1.0 - weight) + values[high] * weight


class LatencyWindow:
    """Quantiles glissants thread-safe (p50/p95/p99) par méthode MCP.

    Complément de l'histogramme Prometheus : celui-ci sert les quantiles
    agrégés (``histogram_quantile``), cette fenêtre sert les surfaces SANS
    PromQL (dashboard interne ``/api/v1/mcp/metrics``, tests) sans introduire
    de cardinalité non bornée : au-delà de ``max_methods`` méthodes connues,
    toute méthode EXOTIQUE est ignorée (l'histogramme, lui, la normalise déjà
    en ``unknown``).
    """

    def __init__(
        self,
        *,
        window: int = _LATENCY_WINDOW_SIZE,
        max_methods: int = _LATENCY_MAX_METHODS,
    ) -> None:
        self._lock = threading.Lock()
        self._samples: dict[str, deque[float]] = {}
        self._window = max(1, int(window))
        self._max_methods = max(1, int(max_methods))

    def observe(self, method: str, duration_seconds: float) -> None:
        """Enregistre une mesure (méthode normalisée, valeur bornée >= 0)."""
        label = normalize_method_label(method)
        value = max(0.0, float(duration_seconds))
        with self._lock:
            samples = self._samples.get(label)
            if samples is None:
                if len(self._samples) >= self._max_methods:
                    return  # borne dure : jamais de croissance illimitée
                samples = deque(maxlen=self._window)
                self._samples[label] = samples
            samples.append(value)

    def quantiles(self) -> dict[str, dict[str, float]]:
        """``{méthode: {count, p50_ms, p95_ms, p99_ms, max_ms}}`` (ms arrondis)."""

        def _round(value: float) -> float:
            return round(value * 1000.0, 3)

        with self._lock:
            snapshot = {label: sorted(values) for label, values in self._samples.items()}
        return {
            label: {
                "count": len(values),
                "p50_ms": _round(_quantile(values, 0.50)),
                "p95_ms": _round(_quantile(values, 0.95)),
                "p99_ms": _round(_quantile(values, 0.99)),
                "max_ms": _round(values[-1]),
            }
            for label, values in snapshot.items()
        }

    @property
    def tracked_methods(self) -> int:
        """Nombre de méthodes suivies (diagnostic / tests)."""
        with self._lock:
            return len(self._samples)

    def reset(self) -> None:
        """Vide la fenêtre (isolation des tests)."""
        with self._lock:
            self._samples.clear()


#: Fenêtre partagée alimentée par ``record_request_latency``.
LATENCY_WINDOW = LatencyWindow()


def normalize_method_label(method: str) -> str:
    """Normalise un label de méthode MCP (cardinalité bornée — ``unknown``)."""
    normalized = str(method or "").strip().lower()
    return normalized if normalized in _ALLOWED_METHOD_LABELS else "unknown"


def latency_quantiles() -> dict[str, dict[str, float]]:
    """Quantiles de latence par méthode (lecture dashboard — jamais bloquante)."""
    return LATENCY_WINDOW.quantiles()


def reset_latency_window() -> None:
    """Réinitialise la fenêtre de latence (isolation des tests)."""
    LATENCY_WINDOW.reset()


def gauge_snapshot() -> dict[str, float]:
    """Valeurs INSTANTANÉES des jauges MCP (lecture dashboard).

    ``prometheus_client`` n'expose pas de getter public sur ``Gauge`` : la
    lecture passe par ``_value.get()`` — même approche que le dashboard interne
    pour ``mcp_runs_active`` / ``mcp_sessions_active`` / HITL.
    """
    return {
        "sessions_active": float(MCP_SESSIONS_ACTIVE._value.get()),
        "sse_streams_active": float(MCP_SSE_STREAMS_ACTIVE._value.get()),
        "runs_active": float(MCP_RUNS_ACTIVE._value.get()),
        "runs_awaiting_approval": float(MCP_RUNS_AWAITING_APPROVAL._value.get()),
    }

# ---------------------------------------------------------------------------
# Tracker de sessions (thread-safe) — alimente la jauge ``mcp_sessions_active``
# ---------------------------------------------------------------------------

_SESSION_TTL_SECONDS = 900.0
_SESSION_SWEEP_MIN_INTERVAL = 30.0


class SessionTracker:
    """Jauge thread-safe des sessions actives avec éviction par TTL.

    Chaque flux SSE actif est compté via ``acquire``/``release``. Pour éviter
    la dérive (client qui meurt sans ``release``), un TTL purges les sessions
    fantômes au prochain ``acquire`` (passe défensive, bornée par intervalle).
    """

    def __init__(self, *, ttl_seconds: float = _SESSION_TTL_SECONDS) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, float] = {}
        self._ttl = max(1.0, float(ttl_seconds))
        self._last_sweep = 0.0

    def acquire(self, session_id: str) -> None:
        """Enregistre une session active (idempotent : ré-acquérir rafraîchit)."""
        now = time.monotonic()
        self._sweep(now)
        with self._lock:
            self._sessions[str(session_id)] = now
            MCP_SESSIONS_ACTIVE.set(len(self._sessions))

    def release(self, session_id: str) -> None:
        """Retire une session (idempotent : sur-libération sans effet)."""
        with self._lock:
            self._sessions.pop(str(session_id), None)
            MCP_SESSIONS_ACTIVE.set(len(self._sessions))

    def _sweep(self, now: float) -> None:
        """Purge les sessions fantômes (TTL) — au plus une passe par intervalle."""
        with self._lock:
            if now - self._last_sweep < _SESSION_SWEEP_MIN_INTERVAL:
                return
            self._last_sweep = now
            expired = [sid for sid, ts in self._sessions.items() if now - ts > self._ttl]
            for sid in expired:
                del self._sessions[sid]
            if expired:
                MCP_SESSIONS_ACTIVE.set(len(self._sessions))

    @property
    def active(self) -> int:
        """Nombre de sessions actives (lecture instantanée)."""
        with self._lock:
            return len(self._sessions)

    def reset(self) -> None:
        """Vide le tracker et remet la jauge à zéro (isolation des tests).

        Le transport n'appelle JAMAIS ``reset`` : la libération passe
        exclusivement par ``release`` (fin de flux) — ``reset`` n'existe que
        pour les tests qui instancient des trackers dédiés.
        """
        with self._lock:
            self._sessions.clear()
            MCP_SESSIONS_ACTIVE.set(0)


#: Instance partagée du tracker (le transport SSE l'utilise pour les jauges).
SESSION_TRACKER = SessionTracker()


def record_tool_call(tool: str, *, is_error: bool = False) -> None:
    """Comptabilise un appel ``tools/call`` (volume + erreurs par tool).

    Cardinalité BORNÉE : le nom est normalisé (minuscules, ``[a-z0-9_.:-]``,
    caractères invalides remplacés par ``_``, 64 caractères max) ; une valeur
    vide retombe sur ``unknown``. Le serveur refusant les tools hors catalogue
    (« Unknown tool »), le nombre de séries suit celui du registre de tools —
    le préfixe tronqué ne peut donc fusionner que des noms pathologiques.
    """
    normalized = _TOOL_LABEL_INVALID_CHARS.sub(
        "_", str(tool or "").strip().lower()
    )[:_TOOL_LABEL_MAX_LENGTH].strip("_")
    label = normalized or "unknown"
    MCP_TOOL_CALLS_TOTAL.labels(tool=label).inc()
    if is_error:
        MCP_TOOL_ERRORS_TOTAL.labels(tool=label).inc()


def record_request_latency(method: str, duration_seconds: float) -> None:
    """Observe la latence d'une requête MCP (histogramme + fenêtre de quantiles).

    Le label est NORMALISÉ (``normalize_method_label`` : méthodes du protocole ou
    ``unknown``) : un identifiant de méthode arbitraire ne peut pas créer de
    série Prometheus. Les p50/p95/p99 sont obtenus par ``histogram_quantile``
    (buckets) et, sans PromQL, par ``latency_quantiles()``.
    """
    label = normalize_method_label(method)
    MCP_REQUEST_LATENCY.labels(method=label).observe(max(0.0, float(duration_seconds)))
    LATENCY_WINDOW.observe(label, duration_seconds)


def record_reconnection(mode: str) -> None:
    """Comptabilise une reconnexion de flux SSE (mode de reprise borné)."""
    MCP_SSE_RECONNECTIONS_TOTAL.labels(
        mode=_bounded(mode, _VALID_RECONNECTION_MODES, "after_sequence")
    ).inc()


def record_rate_limit_rejection() -> None:
    """Comptabilise un rejet par rate limit per-client (compteur dédié).

    Alimente aussi ``mcp_security_rejections_total{reason=rate_limit}`` —
    la dimension rate limit reste visible dans la vue consolidée sécurité.
    """
    MCP_RATE_LIMIT_REJECTIONS_TOTAL.inc()
    record_security_rejection("rate_limit")


def set_awaiting_approval(count: int) -> None:
    """Positionne la jauge des runs en attente d'approbation humaine (HITL)."""
    MCP_RUNS_AWAITING_APPROVAL.set(max(0, int(count)))


def set_active_sessions(count: int) -> None:
    """Positionne directement la jauge des sessions actives (tests / transport)."""
    MCP_SESSIONS_ACTIVE.set(max(0, int(count)))


_VALID_RUN_STATUSES = frozenset(
    {"success", "partial_success", "failed", "awaiting_approval", "in_progress"}
)
_VALID_BACKPRESSURE_SCOPES = frozenset({"global", "client", "quota"})
_VALID_SECURITY_REASONS = frozenset({"scope", "quota", "rate_limit", "policy", "auth", "sse_quota"})
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
    MCP_SSE_INTERRUPTED_TOTAL.labels(reason=str(reason or "unknown").strip() or "unknown").inc()


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
    "LATENCY_BUCKETS_SECONDS",
    "LATENCY_WINDOW",
    "LatencyWindow",
    "MCP_BACKPRESSURE_REJECTIONS_TOTAL",
    "MCP_IDEMPOTENCY_TOTAL",
    "MCP_RATE_LIMIT_REJECTIONS_TOTAL",
    "MCP_REQUEST_LATENCY",
    "MCP_RUNS_ACTIVE",
    "MCP_RUNS_AWAITING_APPROVAL",
    "MCP_RUNS_DEGRADED_TOTAL",
    "MCP_RUNS_RECONCILED_TOTAL",
    "MCP_RUNS_TOTAL",
    "MCP_SECURITY_REJECTIONS_TOTAL",
    "MCP_SESSIONS_ACTIVE",
    "MCP_SSE_INTERRUPTED_TOTAL",
    "MCP_SSE_QUOTA_REJECTIONS_TOTAL",
    "MCP_SSE_RECONNECTIONS_TOTAL",
    "MCP_SSE_STREAMS_ACTIVE",
    "MCP_TOOL_CALLS_TOTAL",
    "MCP_TOOL_ERRORS_TOTAL",
    "SESSION_TRACKER",
    "SessionTracker",
    "gauge_snapshot",
    "latency_quantiles",
    "normalize_method_label",
    "record_backpressure",
    "record_degradation",
    "record_idempotency",
    "record_rate_limit_rejection",
    "record_reconnection",
    "record_request_latency",
    "record_run_status",
    "record_security_rejection",
    "record_sse_quota_rejection",
    "record_stream_interrupted",
    "record_sweeper_action",
    "record_tool_call",
    "reset_latency_window",
    "set_active_runs",
    "set_active_sessions",
    "set_awaiting_approval",
]
