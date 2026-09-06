# project/api/routes/v1/health.py
"""Endpoints de santé v1 (adaptateur HTTP du use-case ``health_usecase``).

    GET /api/v1/health                  photographie de santé (public : healthcheck Docker)
    GET /api/v1/health/model-sanity     sanity check comportemental (public, lecture seule)

Règles HTTP :
    - 200 + rapport si le modèle est sain ; 503 ``model_unhealthy`` sinon
      (``ModelSanityError`` via le handler DomainError global) ;
    - 503 ``model_not_available`` si aucun modèle ne peut être chargé.
Les endpoints sont des ``def`` (threadpool) : l'inférence du sanity check
est CPU-bound et ne doit jamais geler l'event loop.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, Query

from api.dependencies.composition import (
    get_model_repository_port,
    get_prediction_port,
    get_system_status_port,
)
from api.schemas.health import HealthResponse, SanityVerdictResponse
from app.application.health_usecase import run_health_check, run_model_sanity_check
from app.domain.entities.prediction import SanityReport
from app.domain.errors import ModelSanityError
from app.domain.ports.prediction_ports import (
    ModelRepositoryPort,
    PredictionPort,
    SystemStatusPort,
)

router = APIRouter(tags=["Health v1"])


@router.get("/health", response_model=HealthResponse)
def health(
    repository: ModelRepositoryPort = Depends(get_model_repository_port),
    status: SystemStatusPort = Depends(get_system_status_port),
) -> HealthResponse:
    """Statut rapide de l'API : modèle dispo ou non, jobs actifs, maintenance."""
    snapshot = run_health_check(repository=repository, status=status)
    return HealthResponse(**asdict(snapshot))


@router.get("/health/model-sanity", response_model=SanityVerdictResponse)
def model_sanity(
    model_name: str | None = Query(
        None,
        description="Version de modèle à vérifier (dossier sous experiments/models). "
        "Absente : version active.",
    ),
    predictor: PredictionPort = Depends(get_prediction_port),
) -> SanityVerdictResponse:
    """Sanity check comportemental du modèle (SCRUM-74), surface v1.

    Paramètre ``model_name`` — nom sémantique aligné sur le legacy et le
    dashboard (aucun alias ``model`` : un seul nom de contrat, pas deux).
    Le rapport complet est retourné si le modèle est sain ; un modèle non
    entraîné / fallback répond 503 avec le payload domaine standard.
    """
    report: SanityReport = run_model_sanity_check(model_name, predictor=predictor)
    if not report.ok:
        raise ModelSanityError(
            report.detail or f"Modèle non sain [{report.verdict}]",
            details={
                "status": report.status,
                "verdict": report.verdict,
                "min_confidence": report.min_confidence,
                "accuracy": report.accuracy,
                "model": model_name,
            },
        )
    return SanityVerdictResponse(
        verdict=report.verdict,
        status=report.status,
        detail=report.detail,
        min_confidence=report.min_confidence,
        accuracy=report.accuracy,
        model=model_name,
    )
