# project/app/application/training_usecase.py
"""Use-cases du noyau training (Phase 3d — découplage /train).

Extrait la logique métier des handlers legacy ``api/routes/train.py`` :

    - le démarrage délègue au port runner (création du job + thread worker) :
      le use-case ne connaît ni threading, ni SQLite, ni FastAPI ;
    - statut / historique lèvent ``NotFoundError`` (« job_id introuvable »,
      message legacy conservé à l'identique pour la parité des clients) ;
    - la planification délègue au port schedules et mappe ``ValueError``
      (paramétrage cron/interval invalide côté scheduler) en
      ``ValidationError`` (422).

Ports injectés en keyword (même convention que ``predict_usecase``) : les
routes ``api/routes/v1/`` résolvent les ports via la composition root ; les
tests injectent des fakes.
"""

from __future__ import annotations

from app.domain.errors import NotFoundError, ValidationError
from app.domain.ports.training_ports import (
    TrainingJobsPort,
    TrainingRunnerPort,
    TrainingSchedulesPort,
)
from core.models import (
    EpochMetric,
    JobListResponse,
    ScheduledJob,
    ScheduleListResponse,
    ScheduleRequest,
    TrainHistoryResponse,
    TrainJob,
    TrainRequest,
)


def start_training_run(request: TrainRequest, *, runner: TrainingRunnerPort) -> TrainJob:
    """Démarre un entraînement : job PENDING immédiatement retourné (202)."""
    return runner.start(request)


def get_training_job_status(job_id: str, *, jobs: TrainingJobsPort) -> TrainJob:
    """Statut courant d'un job d'entraînement (404 si inconnu)."""
    job = jobs.get(job_id)
    if job is None:
        raise NotFoundError("job_id introuvable")
    return job


def get_training_job_history(job_id: str, *, jobs: TrainingJobsPort) -> TrainHistoryResponse:
    """Historique des métriques par epoch (SCRUM-73) ; liste vide si aucune."""
    if jobs.get(job_id) is None:
        raise NotFoundError("job_id introuvable")
    rows = jobs.metrics(job_id)
    return TrainHistoryResponse(
        job_id=job_id,
        epochs=[EpochMetric(**row) for row in rows],
    )


def cancel_training_run(job_id: str, *, runner: TrainingRunnerPort) -> TrainJob:
    """Annule un entraînement actif (404 si inconnu — cf. adaptateur)."""
    return runner.cancel(job_id)


def list_training_jobs(
    *,
    status: str | None,
    limit: int,
    offset: int,
    jobs: TrainingJobsPort,
    kind: str | None = None,
) -> JobListResponse:
    """Liste paginée et filtrée des jobs (tri ``started_at DESC``).

    ``kind`` (optionnel) restreint le type de job — ``"intent"`` pour
    ``/train/intent/jobs`` ; ``None`` = tous (parité ``/train/jobs`` legacy).
    """
    items, total = jobs.list(status=status, kind=kind, limit=limit, offset=offset)
    return JobListResponse(total=total, items=items, limit=limit, offset=offset)


def schedule_training(
    request: ScheduleRequest, *, schedules: TrainingSchedulesPort
) -> ScheduledJob:
    """Programme un entraînement récurrent (cron OU interval, exclusifs)."""
    try:
        schedule = schedules.create(
            cron=request.cron,
            interval_minutes=request.interval_minutes,
            train_request=request.train.model_dump(),
        )
    except ValueError as err:
        raise ValidationError(str(err)) from err
    return ScheduledJob(**schedule)


def list_training_schedules(*, schedules: TrainingSchedulesPort) -> ScheduleListResponse:
    """Liste des planifications actives + prochaine exécution."""
    items = schedules.list()
    return ScheduleListResponse(total=len(items), items=[ScheduledJob(**s) for s in items])


def delete_training_schedule(schedule_id: str, *, schedules: TrainingSchedulesPort) -> None:
    """Supprime une planification (404 si inconnue)."""
    if not schedules.delete(schedule_id):
        raise NotFoundError("schedule_id introuvable")
