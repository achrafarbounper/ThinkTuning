# project/app/application/intent_training_usecase.py
"""Use-cases de l'entraînement d'intention (Phase 3d-2 — SCRUM-95).

Même mécanique que ``training_usecase`` (noyau sentiment) :

    - le démarrage applique d'abord les validations défensives du legacy
      (dataset présent, version source résoluble — 422 AVANT création du
      job), via ``runner.precheck`` ;
    - la liste des jobs filtre ``kind="intent"`` (store partagé avec le
      sentiment et le pipeline) ;
    - ``activate`` mappe les RuntimeError du store de versions en
      ``ValidationError`` (422, parité legacy).

Le statut RÉUTILISE ``get_training_job_status`` (``training_usecase``) :
même sémantique 404, message legacy « job_id introuvable » conservé.
Le runner d'intention possède ses propres events d'annulation
(``core.intent_trainer``) — distincts du runner sentiment.
"""

from __future__ import annotations

from app.domain.ports.training_ports import (
    IntentTrainingRunnerPort,
    IntentVersioningPort,
    TrainingJobsPort,
)
from core.models import IntentTrainRequest, JobListResponse, TrainJob


def start_intent_training_run(
    request: IntentTrainRequest, *, runner: IntentTrainingRunnerPort
) -> TrainJob:
    """Lance un entraînement d'intention : precheck (422 précoce) puis job."""
    runner.precheck(request)
    return runner.start(request)


def cancel_intent_training_run(
    job_id: str, *, runner: IntentTrainingRunnerPort
) -> TrainJob:
    """Annule un job d'intention (404 si inconnu — cf. adaptateur)."""
    return runner.cancel(job_id)


def list_intent_training_jobs(
    *,
    status: str | None,
    limit: int,
    offset: int,
    jobs: TrainingJobsPort,
) -> JobListResponse:
    """Liste paginée des jobs d'intention uniquement (filtre kind="intent")."""
    items, total = jobs.list(status=status, kind="intent", limit=limit, offset=offset)
    return JobListResponse(total=total, items=items, limit=limit, offset=offset)


def list_intent_model_versions(*, versioning: IntentVersioningPort) -> dict:
    """Versions valides (tri DESC) + pointeur actif (None si aucun modèle)."""
    versions = versioning.list_versions()
    active = versioning.resolve_active_version()
    return {"total": len(versions), "items": versions, "active": active}


def activate_intent_version(
    version: str, *, versioning: IntentVersioningPort
) -> dict:
    """Pointe active.json sur une version existante (422 si inconnue).

    Le classifieur en mémoire n'est PAS rechargé ici : l'IHM chaîne
    POST /classifiers/intent/reload (store et runtime séparés).
    """
    versioning.activate(version)
    return {"status": "activated", "version": version}
