# project/api/routes/intent_train.py

"""Routes d'entraînement du classifieur d'intention (chat/action) — SCRUM-95.

Même pattern de jobs que /train et /pipeline : POST crée un TrainJob persisté
dans core/job_store.py (``kind="intent"``) et exécute
core.intent_trainer.run_intent_training dans un thread daemon ; GET /status et
GET /jobs permettent le suivi par le dashboard (liste filtrée sur kind, donc
sans mélange avec les jobs sentiment/pipeline). L'activation d'une version
réutilise core/intent_store.py (pointeur ``active.json``) ; après activation,
l'IHM chaîne POST /classifiers/intent/reload (store et runtime sont séparés).
"""

import os
import threading
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from api.dependencies.auth import require_api_key
from core.intent_store import (
    list_intent_model_versions,
    resolve_intent_model_dir,
    set_active_intent_version,
)
from core.intent_trainer import (
    cancel_intent_training,
    get_intent_cancel_event,
    run_intent_training,
)
from core.job_store import get_job_store
from core.models import IntentTrainRequest, JobListResponse, JobStatus, TrainJob

router = APIRouter(prefix="/train/intent", tags=["Intent Training"])

_jobs_lock = threading.Lock()


class IntentActivateRequest(BaseModel):
    """Corps de POST /train/intent/activate : version à activer."""

    version: str

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"version": "20260905T120000Z"}]}
    )


@router.post("", response_model=TrainJob, status_code=202)
def start_intent_training(
    req: IntentTrainRequest, _: bool = Depends(require_api_key)
):
    """Lance l'entraînement du classifieur d'intention.

    Réponse 202 avec le job initial (kind="intent") ; suivre avec
    GET /train/intent/status/{job_id}. Validations défensives précoces
    (dataset présent, version source valide) pour répondre 422 avant de
    créer le job.
    """
    if not os.path.isfile(req.dataset_path):
        raise HTTPException(
            status_code=422, detail=f"Dataset introuvable : {req.dataset_path}"
        )
    if req.base_model_version:
        try:
            resolve_intent_model_dir(req.base_model_version)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    job_id = str(uuid.uuid4())
    job = TrainJob(job_id=job_id, status=JobStatus.PENDING, kind="intent")

    store = get_job_store()
    with _jobs_lock:
        store[job_id] = job
        # Pré-crée l'Event d'annulation côté runner (source unique partagée
        # avec POST /train/intent/cancel/{job_id} — plus de dict dupliqué
        # route/runner).
        get_intent_cancel_event(job_id)

    thread = threading.Thread(
        target=run_intent_training, args=(job_id, req), daemon=True
    )
    thread.start()

    return job


@router.get("/status/{job_id}", response_model=TrainJob)
def get_intent_training_status(job_id: str, _: bool = Depends(require_api_key)):
    """Statut d'un job d'entraînement d'intention (404 si job inconnu)."""
    store = get_job_store()
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_id introuvable")
    return job


@router.post("/cancel/{job_id}", response_model=TrainJob)
def cancel_intent_training_endpoint(
    job_id: str, _: bool = Depends(require_api_key)
):
    """Annule un job d'intention (404 si job inconnu)."""
    try:
        return cancel_intent_training(job_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/jobs", response_model=JobListResponse)
def list_intent_training_jobs(
    status: Optional[JobStatus] = Query(
        default=None,
        description="Filtrer par status : pending, running, completed, failed, cancelled",
    ),
    limit: int = Query(default=100, ge=1, le=1000, description="Nombre max de résultats"),
    offset: int = Query(default=0, ge=0, description="Nombre de résultats à ignorer"),
    _: bool = Depends(require_api_key),
):
    """Liste paginée et filtrée des jobs d'intention uniquement (kind="intent").

    Le store est partagé avec /train et /pipeline : le filtre ``kind`` évite
    de mélanger les historiques dans le dashboard.
    """
    store = get_job_store()
    status_value = status.value if status else None
    items, total = store.list_jobs(
        status=status_value, kind="intent", limit=limit, offset=offset
    )
    return JobListResponse(total=total, items=items, limit=limit, offset=offset)


@router.get("/versions")
def list_versions(_: bool = Depends(require_api_key)):
    """Versions d'intention valides + pointeur actif (pour l'IHM).

    ``active`` est la version résolue par défaut (pointeur ``active.json`` ou,
    à défaut, la dernière version valide) ; ``None`` si aucun modèle n'existe
    (le classifieur utilise alors son repli de règles).
    """
    versions = list_intent_model_versions()
    try:
        active_dir = resolve_intent_model_dir()
        active = os.path.basename(os.path.normpath(active_dir))
    except RuntimeError:
        active = None
    return {"total": len(versions), "items": versions, "active": active}


@router.post("/activate")
def activate_intent_version(
    req: IntentActivateRequest, _: bool = Depends(require_api_key)
):
    """Pointe ``active.json`` sur une version d'intention existante (422 sinon).

    Le classifieur en mémoire n'est PAS rechargé ici : le store (active.json)
    et le runtime (classifieur chargé) sont séparés. L'IHM chaîne ensuite
    POST /classifiers/intent/reload pour appliquer la nouvelle version.
    """
    try:
        set_active_intent_version(req.version)
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"status": "activated", "version": req.version}