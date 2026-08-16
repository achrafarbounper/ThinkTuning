"""
API FastAPI pour le projet d'analyse de sentiments multilingue.

Expose deux familles d'endpoints :
  - /train, /train/status/{job_id}   -> lancer et suivre un entraînement
  - /predict, /predict/reload         -> prédire le sentiment de textes

Usage :
    pip install fastapi uvicorn[standard]
    uvicorn api:app --reload --host 0.0.0.0 --port 8000

Documentation interactive une fois lancé : http://localhost:8000/docs
"""

import csv
import io
import os
import threading
import time
import traceback
import uuid
from enum import Enum
from typing import Dict, List, Optional

import torch
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field
from transformers import AutoTokenizer

from src.dataset.loader import load_raw_dataset, augment_dataset
from src.dataset.preprocess import create_dataloaders
from src.model.distilbert import build_model
from src.model.trainer import Trainer, compute_class_weights
from src.utils.config import load_config
from src.inference.predictor import Predictor

MODEL_DIR = "./sentiment_model_final"
CONFIG_PATH = "configs/default.yaml"

app = FastAPI(
    title="Sentiment Analysis API",
    description="Entraînement et prédiction pour l'analyse de sentiments FR/EN",
    version="1.0.0",
)


# ---------------------------------------------------------------------------
# /train
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TrainRequest(BaseModel):
    max_per_lang: int = 500
    augment_fraction: float = 0.4
    variants_per_example: int = 2
    epochs: Optional[int] = None
    batch_size: Optional[int] = None
    num_workers: Optional[int] = None
    max_length: Optional[int] = None
    learning_rate: Optional[float] = None
    weight_decay: Optional[float] = None
    warmup_ratio: Optional[float] = None
    device: str = "auto"


class TrainJob(BaseModel):
    job_id: str
    status: JobStatus
    step: str = "queued"
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    error: Optional[str] = None


# Registre des jobs en mémoire (suffisant pour un usage local / dev).
# Pour un déploiement multi-process, remplacer par Redis ou une DB.
_jobs: Dict[str, TrainJob] = {}
_jobs_lock = threading.Lock()
_job_cancel_events: Dict[str, threading.Event] = {}


def _get_cancel_event(job_id: str) -> threading.Event:
    return _job_cancel_events.setdefault(job_id, threading.Event())


def _is_cancel_requested(job_id: str) -> bool:
    return _job_cancel_events.get(job_id) is not None and _job_cancel_events[job_id].is_set()


def cancel_training(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_id introuvable")

    cancel_event = _get_cancel_event(job_id)
    cancel_event.set()

    if job.status in {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}:
        return job

    job.status = JobStatus.CANCELLED
    job.step = "cancelled"
    job.error = "Training cancelled by user"
    job.finished_at = time.time()
    return job


def _run_training(job_id: str, req: TrainRequest):
    job = _jobs[job_id]
    cancel_event = _get_cancel_event(job_id)
    job.status = JobStatus.RUNNING
    job.started_at = time.time()

    try:
        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            return

        cfg = load_config(CONFIG_PATH)

        overrides = {
            "max_length": req.max_length,
            "learning_rate": req.learning_rate,
            "weight_decay": req.weight_decay,
            "warmup_ratio": req.warmup_ratio,
            "epochs": req.epochs,
            "batch_size": req.batch_size,
            "num_workers": req.num_workers,
        }
        for key, value in overrides.items():
            if value is not None:
                cfg[key] = value

        cfg["device"] = (
            ("cuda" if torch.cuda.is_available() else "cpu")
            if req.device == "auto"
            else req.device
        )

        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            return

        job.step = "loading_dataset"
        raw = load_raw_dataset(max_per_lang=req.max_per_lang)

        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            return

        job.step = "splitting_dataset"
        split = raw.train_test_split(test_size=0.1, seed=42)
        raw_train, raw_val = split["train"], split["test"]

        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            return

        job.step = "augmenting_dataset"
        augmented_train = augment_dataset(
            raw_train,
            variants_per_example=req.variants_per_example,
            augment_fraction=req.augment_fraction,
        )

        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            return

        job.step = "building_dataloaders"
        train_loader, val_loader = create_dataloaders(augmented_train, raw_val, cfg)

        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            return

        job.step = "computing_class_weights"
        class_weights = compute_class_weights(augmented_train["label"])

        job.step = "loading_model"
        tokenizer = AutoTokenizer.from_pretrained(cfg["model_name"])
        model = build_model(cfg)

        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            return

        job.step = "training"
        trainer = Trainer(model, cfg, class_weights=class_weights)
        trainer.train(train_loader, val_loader, cancel_event=cancel_event)

        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            return

        job.step = "saving_model"
        tokenizer.save_pretrained(MODEL_DIR)
        trainer.save(MODEL_DIR)

        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            return

        # Le modèle sur disque a changé : on force le rechargement du
        # Predictor utilisé par /predict au prochain appel.
        with _predictor_lock:
            global _predictor
            _predictor = None

        job.status = JobStatus.COMPLETED
        job.step = "done"

    except RuntimeError as exc:
        if _is_cancel_requested(job_id):
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            job.error = "Training cancelled by user"
        else:
            job.status = JobStatus.FAILED
            job.error = f"{exc}\n{traceback.format_exc()}"
    except Exception as exc:  # noqa: BLE001 - on veut capturer toute erreur pour la remonter via /train/status
        if _is_cancel_requested(job_id):
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            job.error = "Training cancelled by user"
        else:
            job.status = JobStatus.FAILED
            job.error = f"{exc}\n{traceback.format_exc()}"

    finally:
        job.finished_at = time.time()


@app.post("/train", response_model=TrainJob, status_code=202)
def start_training(req: TrainRequest):
    """
    Démarre un entraînement en arrière-plan et renvoie immédiatement un job_id.
    Utiliser GET /train/status/{job_id} pour suivre la progression.
    """
    job_id = str(uuid.uuid4())
    job = TrainJob(job_id=job_id, status=JobStatus.PENDING)

    with _jobs_lock:
        _jobs[job_id] = job
        _job_cancel_events.setdefault(job_id, threading.Event())

    thread = threading.Thread(target=_run_training, args=(job_id, req), daemon=True)
    thread.start()

    return job


@app.post("/train/cancel/{job_id}", response_model=TrainJob)
def cancel_training_endpoint(job_id: str):
    """Demande une annulation coopérative d'un job d'entraînement."""
    return cancel_training(job_id)


@app.get("/train/status/{job_id}", response_model=TrainJob)
def get_training_status(job_id: str):
    job = _jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_id introuvable")
    return job


@app.get("/train/jobs", response_model=List[TrainJob])
def list_training_jobs():
    return list(_jobs.values())


# ---------------------------------------------------------------------------
# /predict
# ---------------------------------------------------------------------------

class PredictRequest(BaseModel):
    texts: List[str] = Field(..., min_length=1, description="Un ou plusieurs textes à analyser")


class PredictionItem(BaseModel):
    text: str
    sentiment: str
    confidence: float


class PredictResponse(BaseModel):
    results: List[PredictionItem]


_predictor: Optional[Predictor] = None
_predictor_lock = threading.Lock()


def _get_predictor() -> Predictor:
    global _predictor
    with _predictor_lock:
        if _predictor is None:
            if not os.path.isdir(MODEL_DIR):
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"Aucun modèle entraîné trouvé dans '{MODEL_DIR}'. "
                        "Lancez d'abord un entraînement via POST /train."
                    ),
                )
            _predictor = Predictor(MODEL_DIR)
        return _predictor


@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest):
    predictor = _get_predictor()
    results = predictor.predict(req.texts)
    return {"results": results}


@app.post("/predict/batch")
async def predict_batch(
    file: UploadFile = File(..., description="CSV file containing one text column."),
    text_column: str = Form("text", description="Name of the column containing the text to predict."),
    response_format: str = Form("json", description="Response format: json or csv."),
):
    """Upload a CSV file and return predictions row by row in JSON or CSV."""
    if response_format not in {"json", "csv"}:
        raise HTTPException(status_code=400, detail="response_format must be 'json' or 'csv'")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="Uploaded file is empty.")

    try:
        text_buffer = io.StringIO(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        try:
            text_buffer = io.StringIO(raw.decode("latin-1"))
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="Unsupported CSV encoding.") from exc

    reader = csv.DictReader(text_buffer)
    if reader.fieldnames is None:
        raise HTTPException(status_code=400, detail="CSV file must contain a header row.")
    if text_column not in reader.fieldnames:
        raise HTTPException(
            status_code=400,
            detail=f"Column '{text_column}' not found. Available columns: {reader.fieldnames}",
        )

    texts = []
    for row in reader:
        value = row.get(text_column)
        if value is None or str(value).strip() == "":
            continue
        texts.append(str(value).strip())

    if not texts:
        raise HTTPException(status_code=400, detail=f"No non-empty values found in column '{text_column}'.")

    predictor = _get_predictor()
    results = predictor.predict(texts)

    if response_format == "json":
        return {"results": results}

    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["row_index", "text", "sentiment", "confidence"])
    for idx, item in enumerate(results, start=1):
        writer.writerow([idx, item["text"], item["sentiment"], item["confidence"]])

    return Response(
        content=csv_buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=predictions.csv"},
    )


@app.post("/predict/reload", status_code=204)
def reload_model():
    """Force le rechargement du modèle depuis le disque au prochain appel à /predict."""
    global _predictor
    with _predictor_lock:
        _predictor = None


# ---------------------------------------------------------------------------
# Divers
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    return {
        "status": "ok",
        "model_available": os.path.isdir(MODEL_DIR),
        "active_jobs": sum(1 for j in _jobs.values() if j.status == JobStatus.RUNNING),
    }