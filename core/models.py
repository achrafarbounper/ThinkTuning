# project/core/models.py

from enum import Enum
from pydantic import BaseModel
from typing import List, Optional, Dict


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TrainRequest(BaseModel):
    max_per_lang: int = 500
    # Chemin optionnel d'un fichier local de corrections manuelles (CSV ou
    # JSONL avec colonnes text, label, lang_code) produit par le workflow
    # d'active learning (SCRUM-56 : merge_reviewed_data.py). Concaténé au
    # dataset HF avant split/augmentation. None => comportement inchangé.
    local_corrections_path: Optional[str] = None
    augment_fraction: float = 0.4
    variants_per_example: int = 2
    class_augment_weights: Optional[Dict[str, float]] = None
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

class JobListResponse(BaseModel):
    """Réponse paginée pour GET /train/jobs."""
    total: int
    items: List[TrainJob]
    limit: int
    offset: int


class ModelVersion(BaseModel):
    name: str
    path: str
    created_at: Optional[float] = None
    active: bool = False