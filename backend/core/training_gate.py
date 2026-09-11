# project/core/training_gate.py
"""Limitation de concurrence des entraînements + file d'attente (P2 lot 16).

Un entraînement charge la stack ML complète (torch + transformers + modèle) :
lancer N runs simultanés sur une petite instance fait fondre la RAM (OOM). Ce
module applique un plafond configurable avec file d'attente bornée :

    - ``TRAIN_MAX_CONCURRENT`` (défaut 1) : nombre de runs ACTIFS autorisés ;
    - ``TRAIN_QUEUE_MAX``      (défaut 2) : requêtes en attente au-delà du
      plafond (first-in-first-out). Au-delà : ``TrainingBusyError`` (429) ;
    - ``TRAIN_QUEUE_TIMEOUT``  (défaut 15 s) : attente max avant 429.

Fonctionnement (thread-safe, aucun I/O) :
    - ``acquire(job_id)`` : prend la place ou s'insère en file d'attente
      (bloquant jusqu'au timeout) ; à la sortie (``release``) le job suivant
      est notifié ;
    - chaque transition est loggée (audit des limites + file d'attente).

Intégré dans ``app/infrastructure/training/training_adapter.py`` (start des
jobs PENDING) — la route v1 et le legacy reçoivent le même 429.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from contextlib import contextmanager

from app.domain.errors import DomainError

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT = 1
DEFAULT_QUEUE_MAX = 2
DEFAULT_QUEUE_TIMEOUT_S = 15.0


class TrainingBusyError(DomainError):
    """Capacité atteinte : la file d'attente est pleine (429 côté API)."""

    code = "train_busy"
    http_status = 429


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw)
        return value if value >= 0 else default
    except (TypeError, ValueError):
        return default


def max_concurrent() -> int:
    """Plafond de runs actifs (0 = illimité, comportement historique)."""
    return _int_env("TRAIN_MAX_CONCURRENT", DEFAULT_MAX_CONCURRENT)


def queue_max() -> int:
    return _int_env("TRAIN_QUEUE_MAX", DEFAULT_QUEUE_MAX)


def queue_timeout() -> float:
    raw = os.getenv("TRAIN_QUEUE_TIMEOUT", "").strip()
    try:
        return max(0.0, float(raw)) if raw else DEFAULT_QUEUE_TIMEOUT_S
    except (TypeError, ValueError):
        return DEFAULT_QUEUE_TIMEOUT_S
class TrainingGate:
    """Compteur de runs actifs + file d'attente FIFO bornée (thread-safe)."""

    def __init__(
        self,
        *,
        max_concurrent_runs: int | None = None,
        max_queue: int | None = None,
        queue_timeout_s: float | None = None,
    ) -> None:
        self.max_concurrent = (
            max_concurrent_runs if max_concurrent_runs is not None else max_concurrent()
        )
        self.max_queue = max_queue if max_queue is not None else queue_max()
        self.timeout_s = queue_timeout_s if queue_timeout_s is not None else queue_timeout()
        self._cond = threading.Condition()
        self._active = 0
        self._queue: deque[str] = deque()

    def _waiting(self) -> int:
        return len(self._queue)

    def acquire(self, job_id: str) -> None:
        """Réserve un slot : immédiat si dispo, sinon file d'attente FIFO.

        Lève ``TrainingBusyError`` si la file est pleine ou après timeout.
        """
        if self.max_concurrent <= 0:
            return  # illimité (comportement historique)
        with self._cond:
            if self._active < self.max_concurrent:
                self._active += 1
                return
            if self._waiting() >= self.max_queue:
                logger.warning(
                    "train busy : file pleine (%d), job %s refusé (429)",
                    self._waiting(),
                    job_id,
                )
                raise TrainingBusyError(
                    f"file d'attente d'entraînement pleine ({self._waiting()})"
                )
            self._queue.append(job_id)
            logger.info(
                "train busy : job %s en file d'attente (position %d)",
                job_id,
                self._waiting(),
            )
            ok = self._cond.wait_for(
                lambda: self._active < self.max_concurrent, timeout=self.timeout_s
            )
            self._queue.popleft()  # retiré de la file (service ou timeout)
            if not ok:
                raise TrainingBusyError(
                    f"timeout d'attente ({self.timeout_s:.0f}s) dépassé pour {job_id}"
                )
            self._active += 1

    def release(self) -> None:
        """Libère un slot et notifie le prochain en file (FIFO)."""
        with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify()

    @contextmanager
    def slot(self, job_id: str):
        """Contexte : réserve à l'entrée, libère à la sortie (y compris erreur)."""
        self.acquire(job_id)
        try:
            yield
        finally:
            self.release()

    @property
    def stats(self) -> dict:
        with self._cond:
            return {
                "active": self._active,
                "waiting": self._waiting(),
                "max_concurrent": self.max_concurrent,
            }


# --- Instance partagée (surchargeable en tests) -------------------------------

_gate: TrainingGate | None = None
_gate_lock = threading.Lock()


def get_training_gate() -> TrainingGate:
    """Gate partagé de l'application (chiffres lus à chaque construction)."""
    global _gate
    with _gate_lock:
        if _gate is None:
            _gate = TrainingGate()
        return _gate


def reset_training_gate(*args, **kwargs) -> TrainingGate:
    """Remplace le gate partagé (isolation des tests / reconfiguration)."""
    global _gate
    with _gate_lock:
        _gate = TrainingGate(*args, **kwargs)
        return _gate


def is_training_limited() -> bool:
    """True si une limite de concurrence est ACTIVÉE (TRAIN_MAX_CONCURRENT>0)."""
    return max_concurrent() > 0


__all__ = [
    "DEFAULT_MAX_CONCURRENT",
    "DEFAULT_QUEUE_MAX",
    "DEFAULT_QUEUE_TIMEOUT_S",
    "TrainingBusyError",
    "TrainingGate",
    "get_training_gate",
    "is_training_limited",
    "max_concurrent",
    "queue_max",
    "queue_timeout",
    "reset_training_gate",
]
