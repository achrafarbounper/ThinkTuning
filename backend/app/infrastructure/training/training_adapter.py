# project/app/infrastructure/training/training_adapter.py
"""Adaptateurs legacy du domaine training (Phase 3d — noyau /train).

Enveloppent les modules ``core`` existants PAR ATTRIBUT DE MODULE (convention
du projet : les monkeypatchs des tests ciblent le module, ils restent donc
efficaces) :

    - ``core.job_store``      : store SQLite persistant des jobs ;
    - ``core.trainer_runner`` : thread worker + événement d'annulation ;
    - ``core.scheduler``      : planifications APScheduler (SCRUM-34).

Les conversions d'erreurs legacy -> domaine sont concentrées ICI :
``RuntimeError`` (annulation d'un job inconnu) devient ``NotFoundError``.
"""

from __future__ import annotations

import threading
import uuid

from app.domain.errors import NotFoundError
from app.domain.ports.training_ports import (
    MetricRows,
    TrainingJobsPort,
    TrainingRunnerPort,
    TrainingSchedulesPort,
)
from core import scheduler as schedule_manager
from core import trainer_runner
from core.job_store import get_job_store
from core.models import JobStatus, TrainJob, TrainRequest

# Verrou d'écriture du store (réplique le comportement du handler legacy :
# mêmes garanties de concurrence entre la route et le worker).
_jobs_lock = threading.Lock()


class ModuleTrainingJobsAdapter:
    """Consultation du store de jobs (par attribut de module)."""

    def get(self, job_id: str) -> TrainJob | None:
        return get_job_store().get(job_id)

    def list(
        self, *, status: str | None, kind: str | None = None, limit: int, offset: int
    ) -> tuple[list[TrainJob], int]:
        return get_job_store().list_jobs(status=status, kind=kind, limit=limit, offset=offset)

    def metrics(self, job_id: str) -> MetricRows:
        return get_job_store().get_job_metrics(job_id)


class ModuleTrainingRunnerAdapter:
    """Démarrage / annulation d'un entraînement (thread worker legacy)."""

    def start(self, request: TrainRequest) -> TrainJob:
        job_id = str(uuid.uuid4())
        job = TrainJob(job_id=job_id, status=JobStatus.PENDING)
        with _jobs_lock:
            get_job_store()[job_id] = job
        # P2 lot 16 (résilience) : plafond de runs concurrents + file d'attente
        # bornée (TRAIN_MAX_CONCURRENT / TRAIN_QUEUE_MAX). Le slot est libéré
        # par la target wrapper à la fin du run (réussite OU échec).
        from core.training_gate import TrainingBusyError, get_training_gate

        gate = get_training_gate()

        def _run_with_slot() -> None:
            try:
                trainer_runner.run_training(job_id, request)
            finally:
                gate.release()

        try:
            gate.acquire(job_id)
        except TrainingBusyError as err:
            # Capacité atteinte : le job PENDING est soldé en FAILED (jamais
            # de zombie) avant la levée — la route legacy/API l'expose en 429.
            job = TrainJob(
                job_id=job_id, status=JobStatus.FAILED, error=str(err)
            )
            with _jobs_lock:
                get_job_store()[job_id] = job
            raise
        thread = threading.Thread(target=_run_with_slot, args=(), daemon=True)
        thread.start()
        return job

    def cancel(self, job_id: str) -> TrainJob:
        try:
            return trainer_runner.cancel_training(job_id)
        except RuntimeError as err:
            raise NotFoundError(str(err) or "job_id introuvable") from err


class ModuleTrainingSchedulesAdapter:
    """Planifications récurrentes (core.scheduler, par attribut de module)."""

    def create(
        self,
        *,
        cron: str | None,
        interval_minutes: int | None,
        train_request: dict,
    ) -> dict:
        return schedule_manager.create_schedule(
            cron=cron, interval_minutes=interval_minutes, train_request=train_request
        )

    def list(self) -> list[dict]:
        return schedule_manager.list_schedules()

    def delete(self, schedule_id: str) -> bool:
        return schedule_manager.delete_schedule(schedule_id)


def build_default_training_jobs() -> TrainingJobsPort:
    return ModuleTrainingJobsAdapter()


def build_default_training_runner() -> TrainingRunnerPort:
    return ModuleTrainingRunnerAdapter()


def build_default_training_schedules() -> TrainingSchedulesPort:
    return ModuleTrainingSchedulesAdapter()
