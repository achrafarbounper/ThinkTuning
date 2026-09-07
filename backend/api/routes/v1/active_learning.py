# project/api/routes/v1/active_learning.py

"""Cycle Active Learning versionné (strangler — Phase 3d-5).

Délégation aux handlers legacy ``api.routes.active_learning`` (parité par
construction) ; auth X-API-Key sur toute la surface (parité legacy).
L'export CSV (``/annotate/export``) n'est PAS migré : non consommé par le
dashboard (discipline strangler — migration au besoin réel).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies.auth import require_api_key
from api.routes import active_learning as legacy
from app.infrastructure.legacy_errors import convert_legacy_http_error
from core.models import (
    ActiveLearningRequest,
    AnnotateListResponse,
    AnnotateRequest,
    CycleRequest,
    MergeAnnotationsResponse,
    TrainJob,
)

router = APIRouter(tags=["Active Learning (v1)"])


def _call_guarded(func, *args):
    try:
        return func(*args, True)
    except HTTPException as exc:
        raise convert_legacy_http_error(exc) from exc


@router.post("/active_learning", status_code=200)
def select_examples(req: ActiveLearningRequest, _: bool = Depends(require_api_key)):
    """Exemples les plus incertains (triés par proximité de confiance à 1/3)."""
    return _call_guarded(legacy.select_examples, req)


@router.post("/annotate", status_code=200)
def annotate(req: AnnotateRequest, _: bool = Depends(require_api_key)):
    """Enregistre une correction manuelle (dédupliquée par texte normalisé)."""
    return _call_guarded(legacy.annotate, req)


@router.get("/annotate/list", response_model=AnnotateListResponse)
def list_annotations(
    limit: int = 100, offset: int = 0, _: bool = Depends(require_api_key)
):
    return _call_guarded(legacy.list_annotations, limit, offset)


@router.post("/annotate/merge", response_model=MergeAnnotationsResponse)
def merge_annotations(output_path: str | None = None, _: bool = Depends(require_api_key)):
    """Fusionne les annotations dans le dataset d'entraînement."""
    return _call_guarded(legacy.merge_annotations, output_path)


@router.post("/active_learning/cycle", response_model=TrainJob, status_code=202)
def start_cycle(req: CycleRequest, _: bool = Depends(require_api_key)):
    """Lance le cycle complet (merge -> retrain -> activation conditionnelle)."""
    return _call_guarded(legacy.start_cycle, req)


@router.get("/active_learning/cycle/status/{job_id}", response_model=TrainJob)
def cycle_status(job_id: str, _: bool = Depends(require_api_key)):
    return _call_guarded(legacy.cycle_status, job_id)
