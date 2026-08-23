# project/api/__init__.py

import os

# === Global flags expected by tests ===
# Définis dans src.utils.flags (source unique) pour éviter les imports
# circulaires : les modules bas niveau (src.*, core.*) lisent ce module
# au lieu d'importer le package api.
from src.utils.flags import TEST_MODE
API_KEY = os.getenv("API_KEY", "dev-local-api-key")

# === Expose FastAPI app ===
from .main import app

# === Expose models ===
from core.models import JobStatus, TrainJob, TrainRequest, ModelVersion, JobListResponse

# === Expose job store ===
from core.job_store import get_job_store, PersistentJobStore
_jobs = get_job_store()
_job_cancel_events = {}

# === Expose training runner ===
from core.trainer_runner import run_training as _run_training, cancel_training

# === Expose predictor ===
from core.predictor_cache import (
    get_predictor as _get_predictor,
    _predictor,
    _predictor_lock,
)

# === Expose config loader ===
from src.utils.config import load_config

# === Expose model roots ===
from core.model_versioning import MODEL_ROOT, MODELS_ROOT

# === Expose rate limit ===
from api.middlewares.rate_limit import (
    RATE_LIMIT_PER_MINUTE,
    _reset_rate_limit_buckets,
)
RATE_LIMIT_ENABLED = RATE_LIMIT_PER_MINUTE > 0

# === Expose maintenance mode ===
from api.middlewares.maintenance import (
    is_maintenance_mode as _MAINTENANCE_MODE,
    set_maintenance_mode,
)

# === Expose API key getter ===
from api.dependencies.auth import _get_api_key

from src.dataset.loader import load_raw_dataset, augment_dataset
from src.dataset.preprocess import create_dataloaders
from src.model.trainer import Trainer, compute_class_weights
from transformers import AutoTokenizer
from src.model.distilbert import build_model
from src.inference.predictor import Predictor
from core.model_versioning import save_model_version
