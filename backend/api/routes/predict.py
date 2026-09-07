import io
import logging
import os
import threading
from typing import Annotated

import pandas as pd
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field, StringConstraints

import api
from api.dependencies.auth import require_api_key
from core.dynamic_batcher import DynamicBatcher
from core.inference_executor import get_executor
from core.predictor_cache import reload_predictor

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

# ---------------------------------------------------------------------------
# DynamicBatcher (Phase 2) : regroupement temporel des requêtes concurrentes.
# Activable par environnement ; le singleton est créé paresseusement (le thread
# worker ne démarre qu'à la première requête /predict/batched).
# ---------------------------------------------------------------------------
BATCHER_ENABLED = os.getenv("BATCHER_ENABLED", "1") == "1"
BATCHER_MAX_BATCH = int(os.getenv("BATCHER_MAX_BATCH", "32"))
BATCHER_WINDOW_MS = float(os.getenv("BATCHER_WINDOW_MS", "20")) / 1000.0
BATCHER_MAX_QUEUE = int(os.getenv("BATCHER_MAX_QUEUE", "512"))

_batcher: DynamicBatcher | None = None
_batcher_lock = threading.Lock()


def _batcher_infer(texts: list[str]) -> list[dict]:
    """Inférence de lot : prédicteur actif (résolution courte à chaque lot)."""
    predictor = api._get_predictor(None)
    return predictor.predict(texts)


def get_predict_batcher() -> DynamicBatcher:
    """Batcher de prédiction singleton (créé paresseusement, thread-safe)."""
    global _batcher
    if _batcher is None:
        with _batcher_lock:
            if _batcher is None:
                _batcher = DynamicBatcher(
                    _batcher_infer,
                    max_batch_size=BATCHER_MAX_BATCH,
                    window_seconds=BATCHER_WINDOW_MS,
                    max_queue=BATCHER_MAX_QUEUE,
                    name="predict-batcher",
                )
    return _batcher


def reset_predict_batcher() -> None:
    """Arrête et réinitialise le batcher (tests / hot-reload)."""
    global _batcher
    with _batcher_lock:
        if _batcher is not None:
            _batcher.stop(wait=True, timeout=5.0)
            _batcher = None


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


class BatchedPredictRequest(BaseModel):
    # Mêmes garde-fous que PredictRequest (bornes globales du module).
    texts: list[Annotated[str, StringConstraints(max_length=MAX_TEXT_CHARS)]] = Field(
        min_length=1,
        max_length=MAX_TEXTS_PER_REQUEST,
    )
    #: True : regroupement dynamique des textes de REQUÊTES CONCURRENTES en un
    #: seul lot d'inférence (latence amortie sous charge). False : inférence
    #: directe via le pool de threads (comportement de /predict).
    use_batcher: bool = True


@router.post("/predict/batched", response_model=PredictResponse)
async def predict_batched(
    req: BatchedPredictRequest,
    _: bool = Depends(require_api_key),
):
    """Prédiction batch asynchrone (Phase 2).

    ``submit()`` du batcher attend sur un ``threading.Event`` : l'antente
    libère le GIL et ne fige PAS l'event loop — les autres requêtes continuent.
    L'ordre des résultats suit l'ordre du corps de requête.
    """
    if req.use_batcher and BATCHER_ENABLED:
        batcher = get_predict_batcher()
        results = [batcher.submit(text) for text in req.texts]
    else:
        predictor = api._get_predictor(None)
        results = await get_executor().run_async(predictor.predict, list(req.texts))
    return {"results": results}


@router.post("/predict", response_model=PredictResponse)
def predict_route(
    req: PredictRequest,
    model: str | None = None,
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
    model: str | None = None,
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
    from core.model_sanity import VERDICT_OK, run_model_sanity

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
