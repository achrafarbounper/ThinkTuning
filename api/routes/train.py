# project/api/routes/train.py

from typing import List

from fastapi import APIRouter, Depends, HTTPException
import threading
import uuid
import time

from api.dependencies.auth import require_api_key
from core.job_store import get_job_store
from core.trainer_runner import run_training, cancel_training
from core.models import TrainRequest, TrainJob, JobStatus

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


@router.post("/cancel/{job_id}", response_model=TrainJob)
def cancel_training_endpoint(job_id: str, _: bool = Depends(require_api_key)):
    return cancel_training(job_id)

@router.get("/jobs", response_model=List[TrainJob])
def list_training_jobs(_: bool = Depends(require_api_key)):
    return list(get_job_store().values())