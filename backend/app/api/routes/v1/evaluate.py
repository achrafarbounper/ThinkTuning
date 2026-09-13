# project/api/routes/v1/evaluate.py
"""Endpoint d'évaluation v1 (Phase 3d-3).

    GET /api/v1/evaluate/confusion?model=&limit=&max_mistakes=

Parité legacy (``api/routes/evaluate.py``) :
    - ``model=None`` => dernière version valide ; 503 enveloppe domaine si
      aucun modèle exploitable (même contrat que /predict) ; 422 si
      l'échantillon de référence est vide ;
    - réponse brute (sans response_model, comme le legacy) :
      {model, n, labels, matrix, metrics{accuracy, f1_macro,
      per_class_recall}, errors_by_class, mistakes, confusion_pairs}.

Auth : scope LECTURE — X-API-Key (admin/read) OU Bearer JWT (read/admin).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from api.dependencies.auth import require_read_api_key_or_jwt
from api.dependencies.composition import get_evaluation_port
from app.application.models_usecase import run_confusion_evaluation
from app.domain.ports.model_versioning_ports import EvaluationPort

router = APIRouter(tags=["Évaluation v1"])


@router.get("/evaluate/confusion")
def get_confusion_v1(
    model: str | None = Query(
        default=None, description="Version à évaluer (None => dernière valide)"
    ),
    limit: int = Query(
        default=300, ge=1, le=2000, description="Exemples par langue de l'échantillon"
    ),
    max_mistakes: int = Query(
        default=100,
        ge=0,
        le=500,
        description="Nombre max d'exemples mal classés renvoyés",
    ),
    _: bool = Depends(require_read_api_key_or_jwt),
    evaluation: EvaluationPort = Depends(get_evaluation_port),
) -> dict:
    """Matrice de confusion + erreurs par classe sur l'échantillon de référence."""
    return run_confusion_evaluation(
        model=model, limit=limit, max_mistakes=max_mistakes, evaluation=evaluation
    )
