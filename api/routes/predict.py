import io
import logging
import os
from typing import Annotated, Optional

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints

from api.dependencies.auth import require_api_key
from core.predictor_cache import reload_predictor
import api

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Prediction"])

# ---------------------------------------------------------------------------
# Garde-fous d'entrée (validation + anti-DoS). Bornes configurables via
# l'environnement pour s'adapter à la capacité mémoire du déploiement.
# ---------------------------------------------------------------------------
MAX_TEXTS_PER_REQUEST = int(os.getenv("PREDICT_MAX_TEXTS", "256"))
MAX_TEXT_CHARS = int(os.getenv("PREDICT_MAX_TEXT_CHARS", "10000"))
MAX_UPLOAD_BYTES = int(os.getenv("PREDICT_BATCH_MAX_BYTES", str(10 * 1024 * 1024)))
MAX_BATCH_ROWS = int(os.getenv("PREDICT_BATCH_MAX_ROWS", "20000"))
PREDICT_CHUNK_SIZE = int(os.getenv("PREDICT_BATCH_CHUNK_SIZE", "128"))


# ---------------------------
# /predict
# ---------------------------

class PredictRequest(BaseModel):
    # min_length/max_length sur la liste : refuse [] (qui ferait planter le
    # tokenizer avec une 500) et borne la taille du lot (anti-DoS).
    texts: list[Annotated[str, StringConstraints(max_length=MAX_TEXT_CHARS)]] = Field(
        min_length=1,
        max_length=MAX_TEXTS_PER_REQUEST,
    )


class Prediction(BaseModel):
    text: str
    sentiment: str
    confidence: float


class PredictResponse(BaseModel):
    results: list[Prediction]


@router.post("/predict", response_model=PredictResponse)
def predict_route(
    req: PredictRequest,
    model: Optional[str] = None,
    _: bool = Depends(require_api_key),
):
    predictor = api._get_predictor(model)
    results = predictor.predict(req.texts)
    return {"results": results}


# ---------------------------
# /predict/batch
# ---------------------------

@router.post("/predict/batch")
async def predict_batch(
    file: UploadFile = File(...),
    text_column: str = Form("text"),
    response_format: str = Form("json"),
    model: Optional[str] = None,
    _: bool = Depends(require_api_key),
):
    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Fichier trop volumineux ({len(raw)} octets, maximum {MAX_UPLOAD_BYTES}).",
        )

    try:
        df = pd.read_csv(io.BytesIO(raw))
    except Exception as exc:
        # `from exc` : la cause réelle (encodage, séparateur…) reste visible
        # dans les logs serveur au lieu d'être avalée silencieusement.
        raise HTTPException(status_code=400, detail="Invalid CSV") from exc

    if text_column not in df.columns:
        raise HTTPException(status_code=400, detail="Missing text column")

    if len(df) > MAX_BATCH_ROWS:
        raise HTTPException(
            status_code=400,
            detail=f"Trop de lignes ({len(df)} > {MAX_BATCH_ROWS}). Découpez le fichier.",
        )

    # Un NaN pandas devient float : le tokenizer planterait sur autre chose
    # que du texte. On refuse explicitement au lieu d'inférer silencieusement.
    series = df[text_column]
    if series.isna().any():
        raise HTTPException(
            status_code=400,
            detail=f"La colonne '{text_column}' contient des valeurs vides.",
        )
    texts = series.astype(str).tolist()

    predictor = api._get_predictor(model)

    # Inférence par chunks : un seul appel predict() sur un gros lot
    # tokenizerait tout en mémoire (OOM). Les résultats restent dans
    # l'ordre du CSV.
    preds: list[dict] = []
    for start in range(0, len(texts), PREDICT_CHUNK_SIZE):
        preds.extend(predictor.predict(texts[start : start + PREDICT_CHUNK_SIZE]))

    df["sentiment"] = [p["sentiment"] for p in preds]
    df["confidence"] = [p["confidence"] for p in preds]
    df["row_index"] = range(len(df))

    # IMPORTANT : ordre des colonnes pour les tests
    df = df[["row_index", text_column, "sentiment", "confidence"]]

    if response_format == "json":
        return {"results": df.to_dict(orient="records")}

    if response_format == "csv":
        csv_bytes = df.to_csv(index=False).encode("utf-8")
        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=predictions.csv"},
        )

    if response_format == "parquet":
        # pyarrow est une dépendance optionnelle de pandas : sans elle,
        # to_parquet lèverait une ImportError non gérée (500).
        try:
            buf = io.BytesIO()
            df.to_parquet(buf, index=False)
        except ImportError as exc:
            raise HTTPException(
                status_code=400,
                detail="Le format 'parquet' nécessite pyarrow (pip install pyarrow).",
            ) from exc
        return Response(
            content=buf.getvalue(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": "attachment; filename=predictions.parquet"},
        )

    raise HTTPException(status_code=400, detail="Invalid response_format")


# ---------------------------
# /predict/reload
# ---------------------------

@router.post("/predict/reload")
def reload_model(_: bool = Depends(require_api_key)):
    reload_predictor()

    # SCRUM-74 : sanity check après rechargement — on refuse de confirmer le
    # rechargement avec un modèle non entraîné / fallback (erreur explicite).
    from core.model_sanity import run_model_sanity, VERDICT_OK

    report = run_model_sanity(api._get_predictor())
    if report["verdict"] != VERDICT_OK:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "reload_rejected",
                "verdict": report["verdict"],
                "detail": report["detail"],
                "min_confidence": report["min_confidence"],
                "accuracy": report["accuracy"],
            },
        )
    return {"status": "reloaded", "sanity": report["verdict"]}


# ---------------------------
# /compare
# ---------------------------

class CompareRequest(BaseModel):
    text_a: str
    text_b: str


@router.post("/compare")
def compare_route(payload: CompareRequest, _: bool = Depends(require_api_key)):
    predictor = api._get_predictor()
    a, b = predictor.predict([payload.text_a, payload.text_b])

    diff = abs(a["confidence"] - b["confidence"])
    identical = a["sentiment"] == b["sentiment"]
    opposed = {a["sentiment"], b["sentiment"]} == {"positive", "negative"}

    return {
        "text_a": a,
        "text_b": b,
        "confidence_diff": round(diff, 2),
        "sentiments_identical": identical,
        "sentiments_opposed": opposed,
        "comparison": "opposed" if opposed else ("identical" if identical else "different"),
    }