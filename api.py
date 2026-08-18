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
import json
import logging
import math
import os
import sqlite3
import threading
import time
import traceback
import uuid
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

import torch
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel, Field
from transformers import AutoTokenizer

from src.dataset.loader import load_raw_dataset, augment_dataset
from src.dataset.preprocess import create_dataloaders
from src.model.distilbert import build_model
from src.model.trainer import Trainer, compute_class_weights
from src.utils.config import load_config
from src.inference.predictor import Predictor

MODEL_ROOT = os.path.join("experiments", "models")
MODEL_DIR = MODEL_ROOT
LEGACY_MODEL_DIR = "./sentiment_model_final"
MODELS_ROOT = MODEL_ROOT
CONFIG_PATH = "configs/default.yaml"
logger = logging.getLogger("thinktuning.api")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
logger.propagate = False

REQUEST_COUNTER = Counter(
    "http_requests_total",
    "Total number of HTTP requests processed by the API.",
    labelnames=("method", "path", "status_code"),
)
REQUEST_LATENCY = Histogram(
    "http_request_duration_seconds",
    "Latency of HTTP requests in seconds.",
    labelnames=("method", "path", "status_code"),
)

API_KEY = os.getenv("API_KEY")
if not API_KEY:
    raise RuntimeError("API_KEY environment variable must be set, even in local development.")

try:
    RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
except ValueError:
    RATE_LIMIT_PER_MINUTE = 60
RATE_LIMIT_ENABLED = RATE_LIMIT_PER_MINUTE > 0
_RATE_LIMIT_LOCK = threading.Lock()

# Maintenance mode state
_MAINTENANCE_MODE = False
_MAINTENANCE_LOCK = threading.Lock()
_MAINTENANCE_MESSAGE = "Service under maintenance. Please try again later."


def _is_rate_limit_enabled():
    return RATE_LIMIT_PER_MINUTE > 0


def _is_maintenance_mode():
    """Check if the service is in maintenance mode."""
    with _MAINTENANCE_LOCK:
        return _MAINTENANCE_MODE


class TokenBucket:
    def __init__(self, rate_per_minute: int):
        self.capacity = max(1, rate_per_minute)
        self.refill_rate = self.capacity / 60.0
        self.tokens = float(self.capacity)
        self.last_update = time.monotonic()

    def consume(self, amount: float = 1.0):
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_update = now

        if self.tokens >= amount:
            self.tokens -= amount
            return True, 0

        wait_seconds = (amount - self.tokens) / self.refill_rate if self.refill_rate > 0 else 0.0
        return False, max(1, int(math.ceil(wait_seconds)))


_RATE_LIMIT_BUCKETS: Dict[str, TokenBucket] = {}


def _reset_rate_limit_buckets():
    with _RATE_LIMIT_LOCK:
        _RATE_LIMIT_BUCKETS.clear()


def _client_identifier(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


def _enforce_rate_limit(request: Request):
    if not _is_rate_limit_enabled() or request.method.upper() != "POST":
        return None

    if request.url.path not in {"/predict", "/predict/batch"}:
        return None

    client_id = _client_identifier(request)
    with _RATE_LIMIT_LOCK:
        bucket = _RATE_LIMIT_BUCKETS.setdefault(client_id, TokenBucket(RATE_LIMIT_PER_MINUTE))
        allowed, wait_seconds = bucket.consume(1.0)
        if allowed:
            return None
        return wait_seconds


# Origines autorisées pour le dashboard React (dashboard-demo.jsx tourne sur
# un port différent de l'API en dev : Vite=5173, CRA=3000). Sans ce
# middleware, le navigateur bloque les requêtes fetch() cross-origin même
# si le X-API-Key est correct. Configurable via la variable d'environnement
# CORS_ALLOWED_ORIGINS (liste séparée par des virgules) pour la prod.
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost,http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000",
    ).split(",")
    if origin.strip()
]


def require_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")
    return True


class ModelVersion(BaseModel):
    name: str
    path: str
    created_at: Optional[float] = None
    active: bool = False


class MaintenanceStatus(BaseModel):
    """Maintenance mode status response."""
    maintenance_mode: bool
    message: Optional[str] = None
    updated_at: float = Field(default_factory=time.time)

app = FastAPI(
    title="Sentiment Analysis API",
    description="Entraînement et prédiction pour l'analyse de sentiments FR/EN",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def maintenance_mode_middleware(request: Request, call_next):
    """Block requests when service is in maintenance mode.
    
    Allows requests to /health, /maintenance endpoints and API key validation endpoint.
    """
    # Allow maintenance-related and health endpoints
    excluded_paths = {
    "/health",
    "/maintenance",
    "/maintenance/enable",
    "/maintenance/disable",
    "/metrics"
    }
    if request.url.path not in excluded_paths and _is_maintenance_mode():
        response = Response(
            content=json.dumps({"detail": _MAINTENANCE_MESSAGE}),
            status_code=503,
            media_type="application/json",
            headers={"Retry-After": "3600"},
        )
        return response
    return await call_next(request)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    wait_seconds = _enforce_rate_limit(request)
    if wait_seconds is not None:
        response = Response(
            content=json.dumps({"detail": "Rate limit exceeded. Please retry later."}),
            status_code=429,
            media_type="application/json",
            headers={"Retry-After": str(wait_seconds)},
        )
        return response
    return await call_next(request)


@app.middleware("http")
async def request_metrics_middleware(request: Request, call_next):
    start_time = time.perf_counter()
    route = request.scope.get("route")
    request_path = getattr(route, "path", request.url.path)
    client_ip = request.client.host if request.client else "unknown"

    try:
        response = await call_next(request)
    except Exception:
        duration = time.perf_counter() - start_time
        status_code = 500
        REQUEST_COUNTER.labels(method=request.method, path=request_path, status_code=str(status_code)).inc()
        REQUEST_LATENCY.labels(method=request.method, path=request_path, status_code=str(status_code)).observe(duration)
        logger.exception(
            "http_request method=%s path=%s status=%s duration_ms=%.3f client_ip=%s",
            request.method,
            request_path,
            status_code,
            duration * 1000,
            client_ip,
        )
        raise

    duration = time.perf_counter() - start_time
    status_code = response.status_code
    REQUEST_COUNTER.labels(method=request.method, path=request_path, status_code=str(status_code)).inc()
    REQUEST_LATENCY.labels(method=request.method, path=request_path, status_code=str(status_code)).observe(duration)
    logger.info(
        "http_request method=%s path=%s status=%s duration_ms=%.3f client_ip=%s",
        request.method,
        request_path,
        status_code,
        duration * 1000,
        client_ip,
    )
    return response


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
    model_path: Optional[str] = None


JOB_STORE_PATH = os.getenv("JOB_STORE_PATH", os.path.join("experiments", "jobs.db"))


class PersistentJobStore(dict):
    """Dict-like job registry backed by SQLite so jobs survive process restarts."""

    def __init__(self, path: str = JOB_STORE_PATH):
        super().__init__()
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._ensure_db()
        self._refresh_from_db()

    def _ensure_db(self):
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )

    def _serialize_job(self, job: TrainJob) -> str:
        payload = job.model_dump() if hasattr(job, "model_dump") else job.dict()
        if isinstance(payload.get("status"), Enum):
            payload["status"] = payload["status"].value
        return json.dumps(payload, default=str)

    def _refresh_from_db(self):
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute("SELECT job_id, payload FROM jobs ORDER BY updated_at DESC").fetchall()
        jobs = {}
        for job_id, payload in rows:
            data = json.loads(payload)
            jobs[job_id] = TrainJob(**data)
        super().clear()
        super().update(jobs)

    def __setitem__(self, key, value):
        if not isinstance(value, TrainJob):
            raise TypeError("PersistentJobStore accepts only TrainJob instances.")
        payload = self._serialize_job(value)
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                """
                INSERT INTO jobs (job_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (key, payload, time.time()),
            )
        super().__setitem__(key, value)

    def __getitem__(self, key):
        if key not in self:
            with sqlite3.connect(self.path) as conn:
                row = conn.execute("SELECT payload FROM jobs WHERE job_id = ?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            data = json.loads(row[0])
            value = TrainJob(**data)
            super().__setitem__(key, value)
        return super().__getitem__(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key):
        if super().__contains__(key):
            return True
        with sqlite3.connect(self.path) as conn:
            return conn.execute("SELECT 1 FROM jobs WHERE job_id = ?", (key,)).fetchone() is not None

    def values(self):
        self._refresh_from_db()
        return super().values()

    def items(self):
        self._refresh_from_db()
        return super().items()

    def __iter__(self):
        self._refresh_from_db()
        return super().__iter__()


_jobs: Dict[str, TrainJob] = PersistentJobStore(path=JOB_STORE_PATH)
_jobs_lock = threading.Lock()
_job_cancel_events: Dict[str, threading.Event] = {}


def _get_job_store() -> PersistentJobStore:
    global _jobs
    if getattr(_jobs, "path", None) != JOB_STORE_PATH:
        _jobs = PersistentJobStore(path=JOB_STORE_PATH)
    return _jobs


def _persist_job(job: TrainJob):
    store = _get_job_store()
    with _jobs_lock:
        if not job.job_id:
            return
        store[job.job_id] = job


def _load_jobs() -> Dict[str, TrainJob]:
    return dict(PersistentJobStore(path=JOB_STORE_PATH))


def cleanup_old_jobs(max_age_days: int = 30, dry_run: bool = True, db_path: Optional[str] = None):
    """Supprime les jobs terminés obsolètes plus vieux qu'un seuil donné.

    Par défaut, le mode est dry-run pour éviter toute suppression accidentelle.
    Les jobs en cours/pending ne sont pas supprimés.
    """
    db_path = db_path or JOB_STORE_PATH
    cutoff_ts = time.time() - max_age_days * 86400
    terminal_statuses = {JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}
    expired_job_ids: List[str] = []

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT job_id, payload, updated_at FROM jobs").fetchall()

    for job_id, payload, updated_at in rows:
        try:
            data = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        status = data.get("status")
        if status in terminal_statuses and float(updated_at) <= cutoff_ts:
            expired_job_ids.append(job_id)

    if dry_run:
        return {
            "deleted": 0,
            "dry_run": True,
            "max_age_days": max_age_days,
            "expires_before": cutoff_ts,
            "job_ids": expired_job_ids,
        }

    if expired_job_ids:
        placeholders = ", ".join("?" for _ in expired_job_ids)
        with sqlite3.connect(db_path) as conn:
            conn.execute(f"DELETE FROM jobs WHERE job_id IN ({placeholders})", tuple(expired_job_ids))

        with _jobs_lock:
            for job_id in expired_job_ids:
                _jobs.pop(job_id, None)
                _job_cancel_events.pop(job_id, None)

    return {
        "deleted": len(expired_job_ids),
        "dry_run": False,
        "max_age_days": max_age_days,
        "expires_before": cutoff_ts,
        "job_ids": expired_job_ids,
    }


def _get_cancel_event(job_id: str) -> threading.Event:
    return _job_cancel_events.setdefault(job_id, threading.Event())


def _is_cancel_requested(job_id: str) -> bool:
    return _job_cancel_events.get(job_id) is not None and _job_cancel_events[job_id].is_set()


def _list_model_versions() -> List[str]:
    os.makedirs(MODEL_ROOT, exist_ok=True)
    if not os.path.isdir(MODEL_ROOT):
        return []

    versions = []
    for entry in sorted(os.listdir(MODEL_ROOT), reverse=True):
        full_path = os.path.join(MODEL_ROOT, entry)
        if os.path.isdir(full_path):
            versions.append(entry)
    return versions


def _resolve_model_dir(model_name: Optional[str] = None) -> str:
    if model_name is not None:
        model_name = model_name.strip()
        if not model_name:
            return _get_latest_model_dir()
        candidate = os.path.join(MODEL_ROOT, model_name)
        if not os.path.isdir(candidate):
            raise HTTPException(
                status_code=404,
                detail=f"Model version '{model_name}' not found in '{MODEL_ROOT}'.",
            )
        return candidate

    versions = _list_model_versions()
    return os.path.join(MODEL_ROOT, versions[0]) if versions else MODEL_ROOT


def _get_latest_model_dir() -> str:
    return _resolve_model_dir()


def _save_model_version(
    tokenizer,
    trainer,
    job_id: Optional[str] = None,
    train_examples: Optional[int] = None,
    val_examples: Optional[int] = None,
    started_at: Optional[float] = None,
    finished_at: Optional[float] = None,
) -> str:
    os.makedirs(MODEL_ROOT, exist_ok=True)
    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    model_dir = os.path.join(MODEL_ROOT, timestamp)
    os.makedirs(model_dir, exist_ok=True)
    tokenizer.save_pretrained(model_dir)
    trainer.save(model_dir)

    epoch_metrics = getattr(trainer, "epoch_metrics", []) or []
    final_metrics = getattr(trainer, "final_metrics", {}) or {}
    hyperparameters = getattr(trainer, "cfg", {}).copy()

    report = {
        "timestamp": timestamp,
        "job_id": job_id,
        "model_dir": model_dir,
        "hyperparameters": hyperparameters,
        "metrics": {
            "accuracy": final_metrics.get("accuracy"),
            "f1_macro": final_metrics.get("f1_macro"),
            "accuracy_by_epoch": [entry.get("accuracy") for entry in epoch_metrics],
            "f1_by_epoch": [entry.get("f1_macro") for entry in epoch_metrics],
            "epochs": len(epoch_metrics),
        },
        "training_duration_seconds": (
            float(finished_at - started_at)
            if started_at is not None and finished_at is not None
            else getattr(trainer, "training_duration_seconds", None)
        ),
        "train_examples": train_examples if train_examples is not None else getattr(trainer, "train_examples", None),
        "val_examples": val_examples if val_examples is not None else getattr(trainer, "val_examples", None),
    }

    report_path = os.path.join(model_dir, "training_report.json")
    with open(report_path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return model_dir


def cancel_training(job_id: str):
    job = _get_job_store().get(job_id)
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
    _persist_job(job)
    return job


def _run_training(job_id: str, req: TrainRequest):
    job = _get_job_store()[job_id]
    cancel_event = _get_cancel_event(job_id)
    job.status = JobStatus.RUNNING
    job.started_at = time.time()

    try:
        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            _persist_job(job)
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
            _persist_job(job)
            return

        job.step = "loading_dataset"
        raw = load_raw_dataset(max_per_lang=req.max_per_lang)

        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            _persist_job(job)
            return

        job.step = "splitting_dataset"
        split = raw.train_test_split(test_size=0.1, seed=42)
        raw_train, raw_val = split["train"], split["test"]

        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            _persist_job(job)
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
            _persist_job(job)
            return

        job.step = "building_dataloaders"
        train_loader, val_loader = create_dataloaders(augmented_train, raw_val, cfg)

        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            _persist_job(job)
            return

        job.step = "computing_class_weights"
        class_weights = compute_class_weights(augmented_train["label"])

        job.step = "loading_model"
        tokenizer = AutoTokenizer.from_pretrained(cfg["model_name"])
        model = build_model(cfg)

        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            _persist_job(job)
            return

        job.step = "training"
        trainer = Trainer(model, cfg, class_weights=class_weights)
        trainer.train(train_loader, val_loader, cancel_event=cancel_event)

        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            _persist_job(job)
            return

        job.step = "saving_model"
        train_examples = len(augmented_train["label"]) if isinstance(augmented_train, dict) and "label" in augmented_train else None
        val_examples = len(raw_val["label"]) if isinstance(raw_val, dict) and "label" in raw_val else None
        model_dir = _save_model_version(
            tokenizer,
            trainer,
            job_id=job_id,
            train_examples=train_examples,
            val_examples=val_examples,
            started_at=job.started_at,
            finished_at=time.time(),
        )
        job.model_path = model_dir

        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            _persist_job(job)
            return

        # Le modèle sur disque a changé : on force le rechargement du
        # Predictor utilisé par /predict au prochain appel.
        with _predictor_lock:
            global _predictor
            _predictor = None

        job.status = JobStatus.COMPLETED
        job.step = "done"
        _persist_job(job)

    except RuntimeError as exc:
        if _is_cancel_requested(job_id):
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            job.error = "Training cancelled by user"
        else:
            job.status = JobStatus.FAILED
            job.error = f"{exc}\n{traceback.format_exc()}"
        _persist_job(job)
    except Exception as exc:  # noqa: BLE001 - on veut capturer toute erreur pour la remonter via /train/status
        if _is_cancel_requested(job_id):
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            job.error = "Training cancelled by user"
        else:
            job.status = JobStatus.FAILED
            job.error = f"{exc}\n{traceback.format_exc()}"
        _persist_job(job)

    finally:
        job.finished_at = time.time()
        _persist_job(job)


@app.post("/train", response_model=TrainJob, status_code=202)
def start_training(req: TrainRequest, _: bool = Depends(require_api_key)):
    """
    Démarre un entraînement en arrière-plan et renvoie immédiatement un job_id.
    Utiliser GET /train/status/{job_id} pour suivre la progression.
    """
    job_id = str(uuid.uuid4())
    job = TrainJob(job_id=job_id, status=JobStatus.PENDING)

    with _jobs_lock:
        _get_job_store()[job_id] = job
        _job_cancel_events.setdefault(job_id, threading.Event())

    thread = threading.Thread(target=_run_training, args=(job_id, req), daemon=True)
    thread.start()

    return job


@app.post("/train/cancel/{job_id}", response_model=TrainJob)
def cancel_training_endpoint(job_id: str, _: bool = Depends(require_api_key)):
    """Demande une annulation coopérative d'un job d'entraînement."""
    return cancel_training(job_id)


@app.get("/train/status/{job_id}", response_model=TrainJob)
def get_training_status(job_id: str, _: bool = Depends(require_api_key)):
    job = _get_job_store().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_id introuvable")
    return job


@app.get("/train/jobs", response_model=List[TrainJob])
def list_training_jobs(_: bool = Depends(require_api_key)):
    return list(_get_job_store().values())


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


def _get_predictor(model_name: Optional[str] = None) -> Predictor:
    global _predictor
    with _predictor_lock:
        target_dir = _resolve_model_dir(model_name)
        if not os.path.isdir(target_dir):
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Aucun modèle entraîné trouvé dans '{target_dir}'. "
                    "Lancez d'abord un entraînement via POST /train."
                ),
            )

        current_dir = getattr(_predictor, "model_path", None)
        if _predictor is None or (
            model_name is not None and os.path.abspath(current_dir) != os.path.abspath(target_dir)
        ):
            _predictor = Predictor(target_dir)
        return _predictor


@app.get("/models", response_model=List[ModelVersion])
def list_models(_: bool = Depends(require_api_key)):
    """Renvoie la liste des modèles enregistrés, du plus récent au plus ancien."""
    active_model_dir = _get_latest_model_dir()
    active_model_path = os.path.abspath(active_model_dir)

    model_versions = []
    for name in _list_model_versions():
        path = os.path.abspath(os.path.join(MODEL_ROOT, name))
        model_versions.append(
            ModelVersion(
                name=name,
                path=path,
                created_at=os.path.getmtime(path),
                active=(path == active_model_path),
            )
        )
    return model_versions


@app.get("/models/{name}/report")
def get_model_report(name: str, _: bool = Depends(require_api_key)):
    """Retourne le rapport JSON associé à une version de modèle."""
    version_dir = os.path.join(MODEL_ROOT, name)
    report_path = os.path.join(version_dir, "training_report.json")
    if not os.path.isfile(report_path):
        raise HTTPException(status_code=404, detail=f"Training report not found for model '{name}'.")

    with open(report_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


@app.post("/predict", response_model=PredictResponse)
def predict(req: PredictRequest, model: Optional[str] = None, _: bool = Depends(require_api_key)):
    predictor = _get_predictor(model)
    results = predictor.predict(req.texts)
    return {"results": results}


@app.post("/predict/batch")
async def predict_batch(
    file: UploadFile = File(..., description="CSV file containing one text column."),
    text_column: str = Form("text", description="Name of the column containing the text to predict."),
    response_format: str = Form("json", description="Response format: json or csv."),
    _: bool = Depends(require_api_key),
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
def reload_model(model: Optional[str] = None, _: bool = Depends(require_api_key)):
    """Force le rechargement du modèle depuis le disque au prochain appel à /predict."""
    global _predictor
    with _predictor_lock:
        _predictor = None


# ---------------------------------------------------------------------------
# Divers
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    active_model_dir = _get_latest_model_dir()
    return {
        "status": "ok",
        "model_available": os.path.isdir(active_model_dir),
        "active_jobs": sum(1 for j in _get_job_store().values() if j.status == JobStatus.RUNNING),
        "model_dir": active_model_dir,
        "maintenance_mode": _is_maintenance_mode(),
    }


# ---------------------------------------------------------------------------
# Maintenance Mode
# ---------------------------------------------------------------------------

@app.get("/maintenance", response_model=MaintenanceStatus)
def get_maintenance_status():
    """Get the current maintenance mode status.
    
    Public endpoint - no API key required.
    """
    with _MAINTENANCE_LOCK:
        return MaintenanceStatus(
            maintenance_mode=_MAINTENANCE_MODE,
            message=_MAINTENANCE_MESSAGE if _MAINTENANCE_MODE else None,
        )


@app.post("/maintenance/enable", response_model=MaintenanceStatus, status_code=200)
def enable_maintenance(
    message: Optional[str] = None,
    _: bool = Depends(require_api_key),
):
    """Enable maintenance mode. Blocks all requests except /health and /maintenance endpoints.
    
    Requires X-API-Key header.
    """
    global _MAINTENANCE_MODE, _MAINTENANCE_MESSAGE
    with _MAINTENANCE_LOCK:
        _MAINTENANCE_MODE = True
        if message:
            _MAINTENANCE_MESSAGE = message
        logger.info(
            "Maintenance mode ENABLED. Message: %s",
            _MAINTENANCE_MESSAGE,
        )
        return MaintenanceStatus(
            maintenance_mode=_MAINTENANCE_MODE,
            message=_MAINTENANCE_MESSAGE,
        )


@app.post("/maintenance/disable", response_model=MaintenanceStatus, status_code=200)
def disable_maintenance(_: bool = Depends(require_api_key)):
    """Disable maintenance mode. Service returns to normal operation.
    
    Requires X-API-Key header.
    """
    global _MAINTENANCE_MODE
    with _MAINTENANCE_LOCK:
        _MAINTENANCE_MODE = False
        logger.info("Maintenance mode DISABLED.")
        return MaintenanceStatus(
            maintenance_mode=_MAINTENANCE_MODE,
            message=None,
        )


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)