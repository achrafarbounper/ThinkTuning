# project/api/routes/v1/intent_training.py
"""Endpoints d'entraînement d'intention v1 (Phase 3d-2 — SCRUM-95).

    POST /api/v1/train/intent                  démarre l'entraînement (202)
    GET  /api/v1/train/intent/status/{job_id}  statut d'un job
    POST /api/v1/train/intent/cancel/{job_id}  annulation
    GET  /api/v1/train/intent/jobs             liste paginée (kind="intent")
    GET  /api/v1/train/intent/versions         versions valides + pointeur actif
    POST /api/v1/train/intent/activate         pointe active.json (422 sinon)

Parité legacy (``api/routes/intent_train.py``) :
    - validations défensives AVANT création du job (dataset introuvable,
      version source invalide => 422 enveloppe domaine) ;
    - /jobs filtre kind="intent" (store partagé avec le sentiment/pipeline) ;
    - /versions renvoie {"total", "items": [str], "active": str|None}
      (sans response_model, comme le legacy — shape documenté ici) ;
    - activate NE recharge PAS le classifieur en mémoire : l'IHM chaîne
      POST /classifiers/intent/reload (store et runtime séparés).

Auth : X-API-Key OU Bearer JWT — GET (status, jobs, versions) en scope
read, POST (start, cancel, activate) en action admin.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from api.dependencies.auth import require_api_key_or_jwt, require_read_api_key_or_jwt
from api.dependencies.composition import (
    get_intent_training_runner_port,
    get_intent_versioning_port,
    get_training_jobs_port,
)
from app.application.intent_training_usecase import (
    activate_intent_version,
    cancel_intent_training_run,
    list_intent_model_versions,
    list_intent_training_jobs,
    start_intent_training_run,
)
from app.application.training_usecase import get_training_job_status
from app.domain.ports.training_ports import (
    IntentTrainingRunnerPort,
    IntentVersioningPort,
    TrainingJobsPort,
)
from core.models import (
    IntentTrainRequest,
    JobListResponse,
    JobStatus,
    TrainJob,
)

router = APIRouter(tags=["Intent Training v1"])


class IntentActivateRequest(BaseModel):
    """Corps de POST /train/intent/activate : version à activer."""

    version: str

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"version": "20260905T120000Z"}]}
    )


@router.post("/train/intent", response_model=TrainJob, status_code=202)
def start_intent_training(
    req: IntentTrainRequest,
    _: bool = Depends(require_api_key_or_jwt),
    runner: IntentTrainingRunnerPort = Depends(get_intent_training_runner_port),
) -> TrainJob:
    """Lance l'entraînement d'intention (job kind="intent", 202)."""
    return start_intent_training_run(req, runner=runner)


@router.get("/train/intent/status/{job_id}", response_model=TrainJob)
def get_intent_training_status(
    job_id: str,
    _: bool = Depends(require_read_api_key_or_jwt),
    jobs: TrainingJobsPort = Depends(get_training_jobs_port),
) -> TrainJob:
    """Statut d'un job d'intention (404 si inconnu — use case partagé)."""
    return get_training_job_status(job_id, jobs=jobs)


@router.post("/train/intent/cancel/{job_id}", response_model=TrainJob)
def cancel_intent_training_endpoint(
    job_id: str,
    _: bool = Depends(require_api_key_or_jwt),
    runner: IntentTrainingRunnerPort = Depends(get_intent_training_runner_port),
) -> TrainJob:
    """Annule un job d'intention (404 si inconnu)."""
    return cancel_intent_training_run(job_id, runner=runner)


@router.get("/train/intent/jobs", response_model=JobListResponse)
def list_intent_training_jobs_endpoint(
    status: JobStatus | None = Query(
        default=None,
        description="Filtrer par status : pending, running, completed, failed, cancelled",
    ),
    limit: int = Query(default=100, ge=1, le=1000, description="Nombre max de résultats"),
    offset: int = Query(default=0, ge=0, description="Nombre de résultats à ignorer"),
    _: bool = Depends(require_read_api_key_or_jwt),
    jobs: TrainingJobsPort = Depends(get_training_jobs_port),
) -> JobListResponse:
    """Liste paginée des jobs d'intention UNIQUEMENT (filtre kind="intent")."""
    status_value = status.value if status else None
    return list_intent_training_jobs(
        status=status_value, limit=limit, offset=offset, jobs=jobs
    )


@router.get("/train/intent/versions")
def list_intent_versions(
    _: bool = Depends(require_read_api_key_or_jwt),
    versioning: IntentVersioningPort = Depends(get_intent_versioning_port),
) -> dict:
    """Versions d'intention valides + pointeur actif : {total, items, active}.

    ``items`` : liste de noms de versions (tri DESC). ``active`` : version
    résolue par défaut (pointeur active.json, sinon la plus récente valide) ;
    ``None`` si aucun modèle n'existe (repli de règles du classifieur).
    """
    return list_intent_model_versions(versioning=versioning)


@router.post("/train/intent/activate")
def activate_intent_version_endpoint(
    req: IntentActivateRequest,
    _: bool = Depends(require_api_key_or_jwt),
    versioning: IntentVersioningPort = Depends(get_intent_versioning_port),
) -> dict:
    """Pointe active.json sur une version existante (422 si inconnue).

    Le classifieur en mémoire n'est PAS rechargé ici : l'IHM chaîne ensuite
    POST /classifiers/intent/reload pour appliquer la nouvelle version.
    """
    return activate_intent_version(req.version, versioning=versioning)
