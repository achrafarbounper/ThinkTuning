# project/api/routes/pipeline.py
"""Pipeline end-to-end (labeling -> filtrage confidence -> fine-tuning LLM).

Même pattern de jobs que /train : POST crée un TrainJob persisté dans
core/job_store.py et exécute core.pipeline_runner.run_pipeline dans un
thread daemon ; GET /status et GET /jobs permettent le suivi par l'UI.
"""

import threading
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies.auth import require_api_key
from core.job_store import get_job_store
from core.pipeline_runner import run_pipeline, cancel_pipeline, get_cancel_event
from core.models import PipelineRequest, TrainJob, JobStatus, JobListResponse

router = APIRouter(prefix="/pipeline", tags=["Pipeline"])

_jobs_lock = threading.Lock()


@router.post("", response_model=TrainJob, status_code=202)
def start_pipeline(req: PipelineRequest, _: bool = Depends(require_api_key)):
    """Lance le pipeline end-to-end (labeling -> filtering -> fine-tuning).

    Réponse 202 avec le job initial ; suivre avec GET /pipeline/status/{job_id}.
    """
    if not req.input_path:
        raise HTTPException(status_code=422, detail="input_path est requis")

    job_id = str(uuid.uuid4())
    job = TrainJob(job_id=job_id, status=JobStatus.PENDING)

    store = get_job_store()
    with _jobs_lock:
        store[job_id] = job
        # Pré-crée l'Event d'annulation côté runner (source unique partagée
        # avec POST /pipeline/cancel/{job_id}) — plus de dict dupliqué route/runner.
        get_cancel_event(job_id)

    thread = threading.Thread(target=run_pipeline, args=(job_id, req), daemon=True)
    thread.start()

    return job


@router.get("/status/{job_id}", response_model=TrainJob)
def get_pipeline_status(job_id: str, _: bool = Depends(require_api_key)):
    store = get_job_store()
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_id introuvable")
    return job


@router.post("/cancel/{job_id}", response_model=TrainJob)
def cancel_pipeline_endpoint(job_id: str, _: bool = Depends(require_api_key)):
    try:
        return cancel_pipeline(job_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/jobs", response_model=JobListResponse)
def list_pipeline_jobs(
    status: Optional[JobStatus] = Query(
        default=None,
        description="Filtrer par status : pending, running, completed, failed, cancelled",
    ),
    limit: int = Query(default=100, ge=1, le=1000, description="Nombre max de résultats"),
    offset: int = Query(default=0, ge=0, description="Nombre de résultats à ignorer"),
    _: bool = Depends(require_api_key),
):
    """Liste paginée et filtrée des jobs pipeline (tri started_at DESC)."""
    store = get_job_store()
    status_value = status.value if status else None
    items, total = store.list_jobs(status=status_value, limit=limit, offset=offset)
    return JobListResponse(total=total, items=items, limit=limit, offset=offset)
