from fastapi import APIRouter, Depends, File, UploadFile, HTTPException, Form
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional
import pandas as pd
import io

from api.dependencies.auth import require_api_key
from core.predictor_cache import get_predictor, reload_predictor
import api


router = APIRouter(tags=["Prediction"])


# ---------------------------
# /predict
# ---------------------------

class PredictRequest(BaseModel):
    texts: list[str]


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

    try:
        df = pd.read_csv(io.BytesIO(raw))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid CSV")

    if text_column not in df.columns:
        raise HTTPException(status_code=400, detail="Missing text column")

    predictor = get_predictor(model)
    preds = predictor.predict(df[text_column].tolist())

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
        buf = io.BytesIO()
        df.to_parquet(buf, index=False)
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
    return {"status": "reloaded"}


# ---------------------------
# /compare
# ---------------------------

class CompareRequest(BaseModel):
    text_a: str
    text_b: str


@router.post("/compare")
def compare_route(payload: CompareRequest, _: bool = Depends(require_api_key)):
    predictor = api._get_predictor()   # ← FIX
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