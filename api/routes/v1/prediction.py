# project/api/routes/v1/prediction.py
"""Endpoints de prédiction v1 (adaptateur HTTP du use-case ``predict_usecase``).

    POST /api/v1/predict          prédiction d'un lot de phrases (protégé API key)
    POST /api/v1/predict/reload   rechargement + validation sanity (protégé API key)

Différences assumées avec le legacy :
    - protégés par ``require_api_key`` (posture production ; le legacy
      /predict est public, la bascule dashboard en tiendra compte) ;
    - erreurs métier via le handler DomainError (payload
      ``{"error": {"code", "message", "details"}}``) ;
    - le lot CSV massif (legacy /predict/batch) et le batcher dynamique ne
      sont PAS encore exposés en v1 : flux à migrer après stabilisation.
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends

from api.dependencies.auth import require_api_key
from api.dependencies.composition import get_prediction_port
from api.schemas.health import ReloadResponse
from api.schemas.prediction import PredictedText, PredictRequest, PredictResponse
from app.application.predict_usecase import PredictCommand, run_predict, run_reload_with_sanity
from app.domain.ports.prediction_ports import PredictionPort

router = APIRouter(tags=["Prediction v1"])


@router.post("/predict", response_model=PredictResponse)
def predict(
    payload: PredictRequest,
    _: bool = Depends(require_api_key),
    predictor: PredictionPort = Depends(get_prediction_port),
) -> PredictResponse:
    """Prédit le sentiment des phrases fournies (ordre préservé)."""
    command = PredictCommand(texts=payload.texts, model_name=payload.model_name)
    results = run_predict(command, predictor=predictor)
    return PredictResponse(
        results=[PredictedText(**asdict(result)) for result in results],
        model_version=results[0].model_version if results else None,
    )


@router.post("/predict/reload", response_model=ReloadResponse)
def reload_model(
    _: bool = Depends(require_api_key),
    predictor: PredictionPort = Depends(get_prediction_port),
) -> ReloadResponse:
    """Recharge la version active et refuse un modèle non sain (SCRUM-74)."""
    report = run_reload_with_sanity(predictor=predictor)
    return ReloadResponse(status="reloaded", sanity=report.verdict)
