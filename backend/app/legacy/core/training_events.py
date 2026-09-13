# project/core/training_events.py

"""Source d'événements d'entraînement pour le flux WebSocket /train/stream.

Abstraction ``TrainingEventsSource`` : l'endpoint WebSocket consomme cette
interface et non le store directement. L'implémentation MongoDB effectue un
polling court, compatible multi-workers et sans état local.
"""

import logging
import os

from core.job_store import get_job_store

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {"completed", "failed", "cancelled"}

# Intervalle de polling (secondes) quand le job est actif (pending/running).
ACTIVE_POLL_SECONDS = 0.5
# Anti-stall : aucun nouvel epoch depuis X minutes sur un job "running" ->
# l'endpoint envoie un événement "stalled" et ferme la connexion (protège
# contre les workers morts et les connexions zombies). Réglable via l'env.
STALL_MINUTES = float(os.getenv("TRAIN_STREAM_STALL_MINUTES", "5"))


def _is_terminal(status) -> bool:
    """True si le statut du job est terminal (str ou enum JobStatus)."""
    value = getattr(status, "value", status)
    return str(value) in TERMINAL_STATUSES


class TrainingEventsSource:
    """Interface du flux d'événements d'entraînement (extensible).

    Un événement est un dict JSON-serializable :
        {"type": "epoch", "job_id": ..., "epoch": N,
         "loss": float|None, "f1_macro": float|None, "accuracy": float|None}
    """

    async def get_new_events(self, job_id: str, last_epoch: int) -> list[dict]:
        """Renvoie les événements d'epoch avec ``epoch > last_epoch``,
        triés par epoch croissant."""
        raise NotImplementedError

    async def get_status(self, job_id: str) -> str | None:
        """Statut du job (pending/running/completed/failed/cancelled)
        ou ``None`` si le job est inconnu."""
        raise NotImplementedError

    async def get_step(self, job_id: str) -> str | None:
        """Étape courante du pipeline (queued, loading_dataset, ..., training,
        saving_model, done) ou ``None`` si le job est inconnu."""
        raise NotImplementedError

    async def get_progress(self, job_id: str) -> dict | None:
        """Avancement temps réel (job.progress) ou ``None`` si absent/inconnu."""
        raise NotImplementedError

    async def get_logs(self, job_id: str, since_seq: int = 0) -> list[dict]:
        """Lignes de log du job avec ``seq > since_seq`` (liste vide si aucune)."""
        raise NotImplementedError

    def is_terminal(self, status) -> bool:
        return _is_terminal(status)


class MongoPollingEventsSource(TrainingEventsSource):
    """Polling des métriques d'entraînement persistées dans MongoDB."""

    def _fetch_events(self, job_id: str, last_epoch: int) -> list[dict]:
        rows = get_job_store().get_job_metrics(job_id)
        events = []
        for row in rows:
            epoch = int(row.get("epoch", 0))
            if epoch <= last_epoch:
                continue
            events.append(
                {
                    "type": "epoch",
                    "job_id": row.get("job_id", job_id),
                    "epoch": epoch,
                    "loss": row.get("loss"),
                    "f1_macro": row.get("f1_macro"),
                    "accuracy": row.get("accuracy"),
                }
            )
        events.sort(key=lambda ev: ev["epoch"])
        return events

    async def get_new_events(self, job_id: str, last_epoch: int) -> list[dict]:
        return self._fetch_events(job_id, last_epoch)

    def get_new_events_sync(self, job_id: str, last_epoch: int) -> list[dict]:
        """Variante synchrone (tests, code non-async)."""
        return self._fetch_events(job_id, last_epoch)

    def _fetch_status(self, job_id: str) -> str | None:
        job = get_job_store().get(job_id)
        if job is None:
            return None
        return getattr(job.status, "value", str(job.status))

    async def get_status(self, job_id: str) -> str | None:
        return self._fetch_status(job_id)

    def get_status_sync(self, job_id: str) -> str | None:
        """Variante synchrone (tests, code non-async)."""
        return self._fetch_status(job_id)

    def _fetch_step(self, job_id: str) -> str | None:
        job = get_job_store().get(job_id)
        if job is None:
            return None
        return getattr(job.step, "value", str(job.step))

    async def get_step(self, job_id: str) -> str | None:
        return self._fetch_step(job_id)

    def get_step_sync(self, job_id: str) -> str | None:
        """Variante synchrone (tests, code non-async)."""
        return self._fetch_step(job_id)

    def _fetch_progress(self, job_id: str) -> dict | None:
        job = get_job_store().get(job_id)
        if job is None:
            return None
        return getattr(job, "progress", None) or None

    async def get_progress(self, job_id: str) -> dict | None:
        return self._fetch_progress(job_id)

    def get_progress_sync(self, job_id: str) -> dict | None:
        """Variante synchrone (tests, code non-async)."""
        return self._fetch_progress(job_id)

    def _fetch_logs(self, job_id: str, since_seq: int = 0) -> list[dict]:
        from core.job_logs import get_logs  # import local : évite un cycle
        return get_logs(job_id, since_seq)

    async def get_logs(self, job_id: str, since_seq: int = 0) -> list[dict]:
        return self._fetch_logs(job_id, since_seq)

    def get_logs_sync(self, job_id: str, since_seq: int = 0) -> list[dict]:
        """Variante synchrone (tests, code non-async)."""
        return self._fetch_logs(job_id, since_seq)


_source: TrainingEventsSource | None = None


def get_training_events_source() -> TrainingEventsSource:
    """Singleton de la source d'événements (comme get_job_store)."""
    global _source
    if _source is None:
        _source = MongoPollingEventsSource()
    return _source


def reset_events_source_for_tests() -> None:
    """Réinitialise le singleton (isolations de tests)."""
    global _source
    _source = None
