# project/api/routes/v1/training.py
"""Endpoints training v1 (Phase 3d — découplage du noyau /train).

    POST   /api/v1/train                        démarre un entraînement (202)
    GET    /api/v1/train/status/{job_id}        statut d'un job
    GET    /api/v1/train/history/{job_id}       métriques par epoch (SCRUM-73)
    POST   /api/v1/train/cancel/{job_id}        annulation
    GET    /api/v1/train/jobs                   liste paginée + filtre statut
    POST   /api/v1/train/schedule               planification récurrente (202, SCRUM-34)
    GET    /api/v1/train/schedules              liste des planifications
    DELETE /api/v1/train/schedules/{schedule_id}  suppression (204)
    WS     /api/v1/train/stream/{job_id}        métriques temps réel (délégation)

Choix assumés (pragmatisme strangler) :
    - les modèles de réponse sont les modèles partagés ``core.models`` (kernel
      avec le worker ``trainer_runner``) : shapes identiques au legacy PAR
      CONSTRUCTION, zéro risque de divergence de contrat ;
    - le WebSocket DÉLÈGUE au handler legacy partagé
      (``api.routes.train.stream_training_metrics``) : logique en source
      unique, et les tests existants monkeypatchent les constantes de CE
      module (``STALL_MINUTES``, ``ACTIVE_POLL_SECONDS``) ;
    - les erreurs métier passent par le handler DomainError global
      (``{"error": {"code", "message", "details"}}``) — le client dashboard
      lit les deux enveloppes (``clientCore._request``) ;
    - parité des middlewares vérifiée : la maintenance s'applique à la v1
      comme au legacy ; le rate-limit ne scope que /predict* (rien à
      répliquer ici).

Auth : X-API-Key OU Bearer JWT (``require_api_key_or_jwt``) — les GET
de consultation (status, history, jobs, schedules) en scope read (un
compte enregistré suit ses entraînements sans lancer d'action), les
POST/DELETE en action admin. Le WebSocket conserve l'auth par query
``?token=`` du handler partagé (JWT accepté, cf. ``ws_is_authorized``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, WebSocket

from api.dependencies.auth import require_api_key_or_jwt, require_read_api_key_or_jwt
from api.dependencies.composition import (
    get_training_jobs_port,
    get_training_runner_port,
    get_training_schedules_port,
)
from api.routes.train import stream_training_metrics
from app.application.training_usecase import (
    cancel_training_run,
    delete_training_schedule,
    get_training_job_history,
    get_training_job_status,
    list_training_jobs,
    list_training_schedules,
    schedule_training,
    start_training_run,
)
from app.domain.ports.training_ports import (
    TrainingJobsPort,
    TrainingRunnerPort,
    TrainingSchedulesPort,
)
from core.models import (
    JobListResponse,
    JobStatus,
    ScheduledJob,
    ScheduleListResponse,
    ScheduleRequest,
    TrainHistoryResponse,
    TrainJob,
    TrainRequest,
)

router = APIRouter(tags=["Training v1"])


@router.post("/train", response_model=TrainJob, status_code=202)
def start_training(
    req: TrainRequest,
    _: bool = Depends(require_api_key_or_jwt),
    runner: TrainingRunnerPort = Depends(get_training_runner_port),
) -> TrainJob:
    """Démarre un entraînement : job PENDING immédiatement retourné."""
    return start_training_run(req, runner=runner)


@router.get("/train/status/{job_id}", response_model=TrainJob)
def get_training_status(
    job_id: str,
    _: bool = Depends(require_read_api_key_or_jwt),
    jobs: TrainingJobsPort = Depends(get_training_jobs_port),
) -> TrainJob:
    """Statut courant d'un job d'entraînement."""
    return get_training_job_status(job_id, jobs=jobs)


@router.get("/train/history/{job_id}", response_model=TrainHistoryResponse)
def get_training_history(
    job_id: str,
    _: bool = Depends(require_read_api_key_or_jwt),
    jobs: TrainingJobsPort = Depends(get_training_jobs_port),
) -> TrainHistoryResponse:
    """Historique des métriques (loss / F1 / accuracy) par epoch."""
    return get_training_job_history(job_id, jobs=jobs)


@router.post("/train/cancel/{job_id}", response_model=TrainJob)
def cancel_training_endpoint(
    job_id: str,
    _: bool = Depends(require_api_key_or_jwt),
    runner: TrainingRunnerPort = Depends(get_training_runner_port),
) -> TrainJob:
    """Annule un entraînement actif (événement d'annulation + statut)."""
    return cancel_training_run(job_id, runner=runner)


@router.get("/train/jobs", response_model=JobListResponse)
def list_training_jobs_endpoint(
    status: JobStatus | None = Query(
        default=None,
        description="Filtrer par status : pending, running, completed, failed, cancelled",
    ),
    limit: int = Query(default=100, ge=1, le=1000, description="Nombre max de résultats"),
    offset: int = Query(default=0, ge=0, description="Nombre de résultats à ignorer"),
    _: bool = Depends(require_read_api_key_or_jwt),
    jobs: TrainingJobsPort = Depends(get_training_jobs_port),
) -> JobListResponse:
    """Liste paginée et filtrée des jobs (tri ``started_at DESC``)."""
    status_value = status.value if status else None
    return list_training_jobs(
        status=status_value, limit=limit, offset=offset, jobs=jobs
    )


@router.post("/train/schedule", response_model=ScheduledJob, status_code=202)
def schedule_training_endpoint(
    req: ScheduleRequest,
    _: bool = Depends(require_api_key_or_jwt),
    schedules: TrainingSchedulesPort = Depends(get_training_schedules_port),
) -> ScheduledJob:
    """Programme un entraînement récurrent (cron 5 champs OU interval_minutes)."""
    return schedule_training(req, schedules=schedules)


@router.get("/train/schedules", response_model=ScheduleListResponse)
def list_training_schedules_endpoint(
    _: bool = Depends(require_read_api_key_or_jwt),
    schedules: TrainingSchedulesPort = Depends(get_training_schedules_port),
) -> ScheduleListResponse:
    """Liste les planifications actives avec leur prochaine exécution."""
    return list_training_schedules(schedules=schedules)


@router.delete("/train/schedules/{schedule_id}", status_code=204, response_model=None)
def delete_training_schedule_endpoint(
    schedule_id: str,
    _: bool = Depends(require_api_key_or_jwt),
    schedules: TrainingSchedulesPort = Depends(get_training_schedules_port),
) -> None:
    """Supprime une planification récurrente (404 si inconnue)."""
    delete_training_schedule(schedule_id, schedules=schedules)


@router.websocket("/train/stream/{job_id}")
async def stream_training_metrics_v1(websocket: WebSocket, job_id: str) -> None:
    """Métriques temps réel — DÉLÉGUE au handler legacy partagé (source unique).

    L'auth ``?token=`` (DASHBOARD_WS_TOKEN, sinon clé API) vit dans le handler
    partagé, exactement comme pour le legacy /train/stream/{job_id}.
    """
    await stream_training_metrics(websocket, job_id)
