# project/api/routes/train.py

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
import threading
import uuid
import time

from api.dependencies.auth import require_api_key
from core.job_store import get_job_store
from core.trainer_runner import run_training, cancel_training
from core import scheduler as schedule_manager
from core.models import (
    TrainRequest,
    TrainJob,
    JobStatus,
    JobListResponse,
    EpochMetric,
    TrainHistoryResponse,
    ScheduleRequest,
    ScheduledJob,
    ScheduleListResponse,
)

router = APIRouter(prefix="/train", tags=["Training"])

_jobs_lock = threading.Lock()
_job_cancel_events = {}


@router.post("", response_model=TrainJob, status_code=202)
def start_training(req: TrainRequest, _: bool = Depends(require_api_key)):
    job_id = str(uuid.uuid4())
    job = TrainJob(job_id=job_id, status=JobStatus.PENDING)

    store = get_job_store()
    with _jobs_lock:
        store[job_id] = job
        _job_cancel_events.setdefault(job_id, threading.Event())

    thread = threading.Thread(target=run_training, args=(job_id, req), daemon=True)
    thread.start()

    return job


@router.get("/status/{job_id}", response_model=TrainJob)
def get_training_status(job_id: str, _: bool = Depends(require_api_key)):
    store = get_job_store()
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_id introuvable")
    return job


@router.get("/history/{job_id}", response_model=TrainHistoryResponse)
def get_training_history(job_id: str, _: bool = Depends(require_api_key)):
    """Historique des métriques d'entraînement (loss / F1 / accuracy) par epoch.

    SCRUM-73 : les métriques sont persistées dans le SQLite existant
    (table ``train_metrics``) pendant l'entraînement. Un job connu mais
    encore sans métriques renvoie une liste vide.
    """
    store = get_job_store()
    if store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="job_id introuvable")
    rows = store.get_job_metrics(job_id)
    return TrainHistoryResponse(
        job_id=job_id,
        epochs=[EpochMetric(**row) for row in rows],
    )


@router.post("/cancel/{job_id}", response_model=TrainJob)
def cancel_training_endpoint(job_id: str, _: bool = Depends(require_api_key)):
    return cancel_training(job_id)

@router.get("/jobs", response_model=JobListResponse)
def list_training_jobs(
    status: Optional[JobStatus] = Query(
        default=None,
        description="Filtrer par status : pending, running, completed, failed, cancelled",
    ),
    limit: int = Query(default=100, ge=1, le=1000, description="Nombre max de résultats"),
    offset: int = Query(default=0, ge=0, description="Nombre de résultats à ignorer"),
    _: bool = Depends(require_api_key),
):
    """Liste paginée et filtrée des jobs d'entraînement.

    - Query params : ``?status=completed&limit=20&offset=0``
    - Tri par ``started_at DESC`` par défaut.
    - Réponse : ``{ total, items, limit, offset }``.
    """
    store = get_job_store()
    status_value = status.value if status else None
    items, total = store.list_jobs(status=status_value, limit=limit, offset=offset)
    return JobListResponse(
        total=total,
        items=items,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# SCRUM-34 : planification récurrente d'entraînements (cron-like, APScheduler)
# ---------------------------------------------------------------------------

@router.post("/schedule", response_model=ScheduledJob, status_code=202)
def schedule_training(req: ScheduleRequest, _: bool = Depends(require_api_key)):
    """Programme un entraînement récurrent.

    - ``cron`` : expression cron à 5 champs, ex. ``"0 2 * * *"`` (chaque jour à 2h).
    - ``interval_minutes`` : intervalle en minutes, ex. ``60`` (toutes les heures).

    Exactement l'un des deux doit être fourni. La planification est persistée
    dans le SQLite existant (table ``scheduled_jobs``) et rechargée au démarrage.
    """
    try:
        schedule = schedule_manager.create_schedule(
            cron=req.cron,
            interval_minutes=req.interval_minutes,
            train_request=req.train.model_dump(),
        )
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return ScheduledJob(**schedule)


@router.get("/schedules", response_model=ScheduleListResponse)
def list_training_schedules(_: bool = Depends(require_api_key)):
    """Liste les planifications d'entraînement actives avec leur prochaine exécution."""
    items = schedule_manager.list_schedules()
    return ScheduleListResponse(total=len(items), items=[ScheduledJob(**s) for s in items])


@router.delete("/schedules/{schedule_id}", status_code=204)
def delete_training_schedule(schedule_id: str, _: bool = Depends(require_api_key)):
    """Supprime une planification récurrente."""
    deleted = schedule_manager.delete_schedule(schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="schedule_id introuvable")
    return None