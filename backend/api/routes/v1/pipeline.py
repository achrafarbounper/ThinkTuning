# project/api/routes/v1/pipeline.py

"""Pipeline end-to-end versionné (strangler — Phase 3d-5).

Même mécanique que le noyau training 3d-1 : délégation aux handlers legacy,
modèles Pydantic partagés (``core.models`` — zéro dérive de DTO), auth
X-API-Key OU Bearer JWT (status/jobs en lecture, start/cancel en action).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from api.dependencies.auth import require_api_key_or_jwt, require_read_api_key_or_jwt
from api.routes import pipeline as legacy
from app.infrastructure.legacy_errors import convert_legacy_http_error
from core.models import JobListResponse, JobStatus, PipelineRequest, TrainJob

router = APIRouter(prefix="/pipeline", tags=["Pipeline (v1)"])


def _call_guarded(func, *args):
    try:
        return func(*args, True)
    except HTTPException as exc:
        raise convert_legacy_http_error(exc) from exc


@router.post("", response_model=TrainJob, status_code=202)
def start_pipeline(req: PipelineRequest, _: bool = Depends(require_api_key_or_jwt)):
    """Lance le pipeline (labeling -> filtrage -> fine-tuning LLM)."""
    return _call_guarded(legacy.start_pipeline, req)


@router.get("/status/{job_id}", response_model=TrainJob)
def get_pipeline_status(job_id: str, _: bool = Depends(require_read_api_key_or_jwt)):
    return _call_guarded(legacy.get_pipeline_status, job_id)


@router.post("/cancel/{job_id}", response_model=TrainJob)
def cancel_pipeline(job_id: str, _: bool = Depends(require_api_key_or_jwt)):
    return _call_guarded(legacy.cancel_pipeline_endpoint, job_id)


@router.get("/jobs", response_model=JobListResponse)
def list_pipeline_jobs(
    status: JobStatus | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    _: bool = Depends(require_read_api_key_or_jwt),
):
    """Liste paginée et filtrée des jobs pipeline (tri started_at DESC)."""
    return _call_guarded(legacy.list_pipeline_jobs, status, limit, offset)
