# project/app/infrastructure/mcp/run_sweeper.py
"""Réconciliation des runs durables MCP (L2 — SCRUM-153).

Un run durable peut rester NON terminal pour toujours si le processus meurt
entre deux transitions (OOM, redéploiement Render, coupure Mongo). Conséquences
observées en production : un run ``running`` fantôme bloque la reprise du même
``run_id``, un lease expiré n'est jamais libéré (le worker suivant est refusé
par ``acquire_lease``), et la jauge ``mcp_runs_active`` dérive indéfiniment.

Ce module ajoute un **sweeper** (thread daemon, une passe par intervalle) qui :

1. **récolte** les runs non terminaux dont ``updated_at`` est plus vieux que
   ``MCP_RUN_STALE_AFTER_SECONDS`` → transition vers ``expired`` (MCP 2.3.0 :
   la PÉREMPTION est un état terminal DÉDIÉ, distinct de l'échec métier),
   avec ``last_error = stale_run_reaped`` et un événement ``orchestrate.degraded``
   persisté (la dégradation est ainsi rejouable et visible côté client) ;
2. **libère** les leases expirés des runs encore actifs (``lease_expires_at``
   dépassé) — la libération est un acte de réconciliation, pas un échec ;
3. **alimente** la jauge ``mcp_runs_active`` (runs non terminaux observés) et le
   compteur ``mcp_runs_reconciled_total{action}``.

Choix d'implémentation :

* **Thread daemon + ``Event``** (et non APScheduler) : le sweeper doit
  fonctionner même si le scheduler applicatif est désactivé (tests, CI,
  worker minimal), et son arrêt doit être immédiat au shutdown ;
* **Une passe ne lève JAMAIS** : chaque run est réconcilié dans son propre
  ``try`` ; une erreur de store (Mongo indisponible) incrémente ``errors`` et
  le cycle suivant réessaie (le sweeper ne peut pas devenir une source de
  panne — il est lui-même un mécanisme de résilience) ;
* **Garde-fou anti-zombie** : ``partial_success`` est traité comme un
  ABOUTISSEMENT (jamais récolté) — il reste reprenable volontairement par le
  client, ce n'est pas un état bloqué ;
* **``awaiting_approval``** : attente HUMAINE légitime → grâce séparée et plus
  longue (``MCP_RUN_AWAITING_APPROVAL_GRACE_SECONDS``) avant récolte.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.domain.ports.mcp_ports import MCPDurableRunState, MCPDurableRunStorePort
from app.infrastructure.mcp import mcp_metrics
from app.infrastructure.mcp.mcp_events import (
    DEGRADATION_LEASE_EXPIRED,
    DEGRADATION_STALE_RUN,
    EVENT_DEGRADED,
)

logger = logging.getLogger("thinktuning.mcp.sweeper")

# ---------------------------------------------------------------------------
# États
# ---------------------------------------------------------------------------

#: États strictement terminaux de la FSM durable (MCP 2.3.0 : ``expired`` est
#: le terminal de PÉREMPTION — récolte du sweeper, réessayable via retry).
TERMINAL_RUN_STATES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "expired"}
)

#: États ABOUTIS (y compris ``partial_success``) : jamais récoltés — un
#: ``partial_success`` reste reprenable à la demande du client.
SETTLED_RUN_STATES: frozenset[str] = frozenset(
    {"completed", "failed", "cancelled", "expired", "partial_success"}
)

#: États candidats à la récolte s'ils sont périmés.
REAPABLE_RUN_STATES: frozenset[str] = frozenset({"pending", "running", "awaiting_approval"})

DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_STALE_AFTER_SECONDS = 900
DEFAULT_AWAITING_APPROVAL_GRACE_SECONDS = 3600
DEFAULT_LIST_LIMIT = 200

ACTION_STALE_RUN_REAPED = DEGRADATION_STALE_RUN
ACTION_LEASE_EXPIRED = DEGRADATION_LEASE_EXPIRED


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _to_utc(value: datetime | None) -> datetime | None:
    """Normalise un datetime (naïf = UTC) — certains stores relisent sans tzinfo."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass
class SweepReport:
    """Bilan d'une passe (sérialisable : exploité par les logs et les tests)."""

    at: datetime
    scanned: int = 0
    stale_reaped: int = 0
    leases_released: int = 0
    active: int = 0
    skipped: int = 0
    errors: int = 0
    error_details: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "scanned": self.scanned,
            "stale_reaped": self.stale_reaped,
            "leases_released": self.leases_released,
            "active": self.active,
            "skipped": self.skipped,
            "errors": self.errors,
            "error_details": list(self.error_details),
        }


# ---------------------------------------------------------------------------
# Sweeper
# ---------------------------------------------------------------------------


class RunSweeper:
    """Réconcilie périodiquement l'état des runs durables (thread daemon)."""

    def __init__(
        self,
        store: MCPDurableRunStorePort,
        *,
        stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
        awaiting_approval_grace_seconds: int = DEFAULT_AWAITING_APPROVAL_GRACE_SECONDS,
        limit: int = DEFAULT_LIST_LIMIT,
        interval_seconds: int = DEFAULT_INTERVAL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self.stale_after_seconds = max(1, int(stale_after_seconds))
        # Une attente d'approbation est HUMAINE : la grâce ne peut pas être
        # plus courte que le seuil de péremption général.
        self.awaiting_approval_grace_seconds = max(
            self.stale_after_seconds, int(awaiting_approval_grace_seconds)
        )
        self.limit = max(1, min(int(limit), 200))
        self.interval_seconds = max(1, int(interval_seconds))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._last_report: SweepReport | None = None

    # -- introspection ------------------------------------------------------
    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def last_report(self) -> SweepReport | None:
        with self._lock:
            return self._last_report

    def status(self) -> dict[str, Any]:
        report = self.last_report
        return {
            "running": self.is_running,
            "interval_seconds": self.interval_seconds,
            "stale_after_seconds": self.stale_after_seconds,
            "awaiting_approval_grace_seconds": self.awaiting_approval_grace_seconds,
            "last_report": report.as_dict() if report is not None else None,
        }

    # -- cycle --------------------------------------------------------------
    def sweep_once(self, now: datetime | None = None) -> SweepReport:
        """Une passe de réconciliation — ne lève JAMAIS (défensif par contrat)."""
        current = _to_utc(now) or self._clock()
        report = SweepReport(at=current)
        try:
            runs = self._store.list_runs(limit=self.limit)
        except Exception as exc:  # store indisponible : le prochain cycle réessaie
            report.errors += 1
            report.error_details = (f"list_runs: {exc}",)
            logger.warning("Sweeper MCP : lecture des runs impossible (%s)", exc)
            self._publish(report)
            return report

        active = 0
        for state in runs:
            report.scanned += 1
            state_name = str(state.state or "").strip().lower()
            if state_name not in SETTLED_RUN_STATES:
                active += 1
            try:
                if self._release_expired_lease(state, current):
                    report.leases_released += 1
                if state_name in SETTLED_RUN_STATES:
                    report.skipped += 1
                    continue
                if state_name not in REAPABLE_RUN_STATES:
                    report.skipped += 1
                    continue
                if not self._is_stale(state, current):
                    report.skipped += 1
                    continue
                self._reap(state, reason=ACTION_STALE_RUN_REAPED)
                report.stale_reaped += 1
            except Exception as exc:
                report.errors += 1
                report.error_details = (*report.error_details, f"{state.run_id}: {exc}")
                logger.warning(
                    "Sweeper MCP : réconciliation du run %s impossible (%s)",
                    state.run_id,
                    exc,
                )
        report.active = active
        self._publish(report)
        if report.stale_reaped or report.leases_released or report.errors:
            logger.info(
                "Sweeper MCP : scanned=%s reaped=%s leases_released=%s active=%s errors=%s",
                report.scanned,
                report.stale_reaped,
                report.leases_released,
                report.active,
                report.errors,
            )
        return report

    # -- décisions ----------------------------------------------------------
    def _is_stale(self, state: MCPDurableRunState, now: datetime) -> bool:
        updated = _to_utc(state.updated_at)
        if updated is None:
            return False
        age_seconds = (now - updated).total_seconds()
        threshold = (
            self.awaiting_approval_grace_seconds
            if str(state.state or "").strip().lower() == "awaiting_approval"
            else self.stale_after_seconds
        )
        return age_seconds > threshold

    def _lease_expired(self, state: MCPDurableRunState, now: datetime) -> bool:
        if not state.lease_owner:
            return False
        expires_at = _to_utc(state.lease_expires_at)
        return expires_at is not None and expires_at <= now

    def _release_expired_lease(self, state: MCPDurableRunState, now: datetime) -> bool:
        """Libère un lease expiré (idempotent : ``False`` si rien à faire)."""
        if not self._lease_expired(state, now):
            return False
        owner = str(state.lease_owner or "")
        released = self._store.release_lease(state.run_id, owner)
        logger.info(
            "Sweeper MCP : lease expiré libéré (run_id=%s owner=%s état=%s)",
            state.run_id,
            owner,
            getattr(released, "state", None),
        )
        mcp_metrics.record_sweeper_action(ACTION_LEASE_EXPIRED)
        return True

    def _reap(self, state: MCPDurableRunState, *, reason: str) -> None:
        """Réconcilie un run périmé vers ``expired`` (MCP 2.3.0).

        La péremption n'est PAS un échec métier : tout run récolté
        (``pending`` / ``running`` / ``awaiting_approval``) bascule vers l'état
        terminal DÉDIÉ ``expired`` avec ``last_error`` = cause de récolte. Un
        run ``expired`` reste visible côté client (``runs/get``) et RÉESSAYABLE
        via ``runs/retry`` (nouveau run lié — jamais de ré-exécution implicite).
        """
        state_name = str(state.state or "").strip().lower()
        self._store.transition(
            state.run_id,
            "expired",
            failure_phase=state.failure_phase,
            last_error=reason,
        )
        self._append_degradation_event(state, reason=reason)
        mcp_metrics.record_sweeper_action(ACTION_STALE_RUN_REAPED)
        mcp_metrics.record_degradation(reason)
        logger.warning(
            "Sweeper MCP : run périmé réconcilié (run_id=%s état=%s -> terminal, raison=%s)",
            state.run_id,
            state_name,
            reason,
        )

    def _append_degradation_event(self, state: MCPDurableRunState, *, reason: str) -> None:
        """Persiste ``orchestrate.degraded`` (traçabilité + replay côté client).

        Best-effort : un store qui n'accepte pas l'événement ne doit pas faire
        échouer la réconciliation d'état (qui, elle, est le contrat).
        """
        try:
            self._store.append_event(
                state.run_id,
                {
                    "event": EVENT_DEGRADED,
                    "reason": reason,
                    "phase": state.phase,
                    "failure_phase": state.failure_phase,
                    "run_id": state.run_id,
                    "degraded": True,
                    "source": "run_sweeper",
                },
            )
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug(
                "Sweeper MCP : événement de dégradation non persisté (run_id=%s, %s)",
                state.run_id,
                exc,
            )

    def _publish(self, report: SweepReport) -> None:
        """Publie le bilan (métriques + dernier rapport) — jamais bloquant."""
        with self._lock:
            self._last_report = report
        mcp_metrics.set_active_runs(report.active)

    # -- cycle de vie thread ------------------------------------------------
    def start(self) -> bool:
        """Démarre le thread daemon (idempotent : ``False`` s'il tourne déjà)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop_event.clear()
            thread = threading.Thread(
                target=self._loop,
                name="mcp-run-sweeper",
                daemon=True,
            )
            self._thread = thread
        thread.start()
        logger.info(
            "Sweeper MCP démarré (interval=%ss seuil_péremption=%ss grâce_approbation=%ss)",
            self.interval_seconds,
            self.stale_after_seconds,
            self.awaiting_approval_grace_seconds,
        )
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        """Arrête le thread (idempotent) — utilisé au shutdown de l'API."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, float(timeout)))
        with self._lock:
            self._thread = None
        return thread is None or not thread.is_alive()

    def _loop(self) -> None:
        """Boucle de fond : une passe par intervalle, jamais d'exception remontée."""
        while not self._stop_event.wait(self.interval_seconds):
            try:
                self.sweep_once()
            except Exception:  # pragma: no cover - filet de sécurité
                logger.exception("Sweeper MCP : passe de réconciliation interrompue")


# ---------------------------------------------------------------------------
# Intégration applicative (lifespan FastAPI) — injectable pour les tests
# ---------------------------------------------------------------------------

_sweeper: RunSweeper | None = None
_sweeper_lock = threading.Lock()
_resolved_store: MCPDurableRunStorePort | None = None


def is_run_sweeper_enabled() -> bool:
    """``MCP_RUN_SWEEPER_ENABLED=0`` désactive le sweeper (tests, worker minimal)."""
    return os.getenv("MCP_RUN_SWEEPER_ENABLED", "1").strip().lower() not in {"0", "false", "no"}


def resolve_durable_run_store() -> MCPDurableRunStorePort:
    """Résout le store durable (même source que le transport SSE), une seule fois.

    ``get_mcp_durable_run_store`` peut instancier un client Mongo : on mémorise
    donc l'instance résolue pour ne pas en créer une par cycle.
    """
    global _resolved_store
    with _sweeper_lock:
        if _resolved_store is not None:
            return _resolved_store
    from app.infrastructure.mcp.mcp_server_sse import get_mcp_durable_run_store

    store = get_mcp_durable_run_store()
    with _sweeper_lock:
        _resolved_store = store
    return store


def get_run_sweeper() -> RunSweeper | None:
    with _sweeper_lock:
        return _sweeper


def start_run_sweeper(
    store: MCPDurableRunStorePort | None = None,
    **overrides: Any,
) -> RunSweeper | None:
    """Démarre le sweeper (idempotent) — ne lève JAMAIS au démarrage.

    Un sweeper indisponible (store non configuré, pymongo absent) est un
    problème d'exploitation LOGGÉ, jamais un échec de démarrage de l'API :
    l'API reste pleinement fonctionnelle sans réconciliation de fond.
    """
    global _sweeper
    if not is_run_sweeper_enabled():
        logger.info("Sweeper MCP désactivé (MCP_RUN_SWEEPER_ENABLED=0)")
        return None
    with _sweeper_lock:
        existing = _sweeper
    if existing is not None and existing.is_running:
        return existing
    try:
        resolved = store if store is not None else resolve_durable_run_store()
        options: dict[str, Any] = {
            "stale_after_seconds": _env_int(
                "MCP_RUN_STALE_AFTER_SECONDS", DEFAULT_STALE_AFTER_SECONDS
            ),
            "awaiting_approval_grace_seconds": _env_int(
                "MCP_RUN_AWAITING_APPROVAL_GRACE_SECONDS",
                DEFAULT_AWAITING_APPROVAL_GRACE_SECONDS,
            ),
            "interval_seconds": _env_int(
                "MCP_RUN_SWEEPER_INTERVAL_SECONDS", DEFAULT_INTERVAL_SECONDS
            ),
        }
        options.update(overrides)
        sweeper = RunSweeper(resolved, **options)
        sweeper.start()
    except Exception as exc:
        logger.warning("Sweeper MCP non démarré (%s)", exc)
        return None
    with _sweeper_lock:
        _sweeper = sweeper
    return sweeper


def stop_run_sweeper(timeout: float = 5.0) -> None:
    """Arrête le sweeper global (shutdown de l'API) — sans lever."""
    global _sweeper
    with _sweeper_lock:
        sweeper = _sweeper
        _sweeper = None
    if sweeper is None:
        return
    try:
        sweeper.stop(timeout=timeout)
    except Exception as exc:  # pragma: no cover - défensif
        logger.warning("Arrêt du sweeper MCP imparfait (%s)", exc)


def run_sweeper_status() -> dict[str, Any] | None:
    """État du sweeper (diagnostic / supervision) ou ``None`` s'il est arrêté."""
    sweeper = get_run_sweeper()
    return sweeper.status() if sweeper is not None else None


__all__ = [
    "ACTION_LEASE_EXPIRED",
    "ACTION_STALE_RUN_REAPED",
    "DEFAULT_AWAITING_APPROVAL_GRACE_SECONDS",
    "DEFAULT_INTERVAL_SECONDS",
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_STALE_AFTER_SECONDS",
    "REAPABLE_RUN_STATES",
    "SETTLED_RUN_STATES",
    "TERMINAL_RUN_STATES",
    "RunSweeper",
    "SweepReport",
    "get_run_sweeper",
    "is_run_sweeper_enabled",
    "resolve_durable_run_store",
    "run_sweeper_status",
    "start_run_sweeper",
    "stop_run_sweeper",
]
