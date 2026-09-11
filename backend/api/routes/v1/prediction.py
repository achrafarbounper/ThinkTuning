# project/api/routes/v1/prediction.py
"""Endpoints de prédiction v1 (adaptateur HTTP du use-case ``predict_usecase``).

    POST /api/v1/predict            prédiction d'un lot de phrases (protégé API key)
    POST /api/v1/predict/batch      prédiction CSV multipart → JSON/CSV/parquet
    POST /api/v1/predict/reload     rechargement + validation sanity (protégé API key)

Différences assumées avec le legacy :
    - erreurs métier via le handler DomainError (payload
      ``{"error": {"code", "message", "details"}}``) — sauf les statuts sans
      équivalent (ex. 413) qui sont re-levés tels quels (parité totale) ;
    - ``/predict/batch`` délègue au handler legacy ``api.routes.predict`` :
      la signature FastAPI multipart est rejouée à l'identique, le framework
      parse le fichier/les forms, l'appelé fait le reste — parité par
      construction (chunks, ordre des colonnes, formats json/csv/parquet).

Auth : PARITÉ avec le legacy — vérifié dans ``api/routes/predict.py``,
``/predict``, ``/predict/batch`` et ``/predict/reload`` legacy portent le même
``Depends(require_api_key)`` (la v1 n'introduit aucune exigence nouvelle).
"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from api.dependencies.auth import require_api_key, require_read_api_key
from api.dependencies.composition import get_prediction_port
from api.routes import predict as legacy_predict
from api.schemas.health import ReloadResponse
from api.schemas.prediction import PredictedText, PredictRequest, PredictResponse
from app.application.predict_usecase import PredictCommand, run_predict, run_reload_with_sanity
from app.domain.ports.prediction_ports import PredictionPort
from app.infrastructure.legacy_errors import convert_legacy_http_error

router = APIRouter(tags=["Prediction v1"])


@router.post("/predict", response_model=PredictResponse)
def predict(
    payload: PredictRequest,
    _: bool = Depends(require_read_api_key),  # P1 : lecture (infra hexagonale)
    predictor: PredictionPort = Depends(get_prediction_port),
) -> PredictResponse:
    """Prédit le sentiment des phrases fournies (ordre préservé)."""
    command = PredictCommand(texts=payload.texts, model_name=payload.model_name)
    results = run_predict(command, predictor=predictor)
    return PredictResponse(
        results=[PredictedText(**asdict(result)) for result in results],
        model_version=results[0].model_version if results else None,
    )


@router.post("/predict/batch")
async def predict_batch(
    file: UploadFile = File(...),
    text_column: str = Form("text"),
    response_format: str = Form("json"),
    model: str | None = None,
    _: bool = Depends(require_read_api_key),  # P1 : lecture
):
    """Prédit un CSV uploadé et renvoie JSON, CSV ou parquet (délégation legacy)."""
    try:
        return await legacy_predict.predict_batch(
            file, text_column, response_format, model, True
        )
    except HTTPException as exc:
        raise convert_legacy_http_error(exc) from exc


@router.post("/predict/reload", response_model=ReloadResponse)
def reload_model(
    _: bool = Depends(require_api_key),
    predictor: PredictionPort = Depends(get_prediction_port),
) -> ReloadResponse:
    """Recharge la version active et refuse un modèle non sain (SCRUM-74)."""
    report = run_reload_with_sanity(predictor=predictor)
    return ReloadResponse(status="reloaded", sanity=report.verdict)
