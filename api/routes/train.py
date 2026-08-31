# project/api/routes/train.py

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
import threading
import uuid
import time

from api.dependencies.auth import require_api_key
from core.job_store import get_job_store
from core.trainer_runner import run_training, cancel_training
from core.models import (
    TrainRequest,
    TrainJob,
    JobStatus,
    JobListResponse,
    EpochMetric,
    TrainHistoryResponse,
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