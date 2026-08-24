# project/core/models.py

from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator
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
    # Exemple affiché/pré-rempli dans Swagger UI ("Try it out") : sans lui,
    # les dicts libres sont pré-remplis avec des placeholders 'additionalProp1'
    # qui font planter l'entraînement s'ils sont envoyés tels quels.
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "max_per_lang": 500,
                    "local_corrections_path": None,
                    "augment_fraction": 0.4,
                    "variants_per_example": 2,
                    "class_augment_weights": {"0": 1.0, "1": 2.0, "2": 1.0},
                    "epochs": 3,
                    "batch_size": 16,
                    "num_workers": None,
                    "max_length": None,
                    "learning_rate": None,
                    "weight_decay": None,
                    "warmup_ratio": None,
                    "device": "auto",
                }
            ]
        }
    )

    @field_validator("class_augment_weights")
    @classmethod
    def validate_class_augment_weights(cls, v: Optional[Dict[str, float]]) -> Optional[Dict[str, float]]:
        """Rejette tôt (HTTP 422) les clés non numériques et les poids négatifs.

        Les clés 'additionalProp1', 'additionalProp2'... sont les placeholders
        générés par Swagger UI pour les dicts libres : elles arrivaient jusque
        dans augment_dataset() et provoquaient un ValueError obscur en plein
        job d'entraînement au lieu d'une 422 immédiate côté API.
        """
        if v is None:
            return v
        cleaned: Dict[str, float] = {}
        for key, weight in v.items():
            try:
                label = int(str(key).strip())
            except (TypeError, ValueError):
                raise ValueError(
                    f"Clé invalide dans class_augment_weights : {key!r}. "
                    "Attendu un label de classe entier (ex. 0, 1, 2). Les clés "
                    "'additionalProp1', 'additionalProp2'... sont des valeurs "
                    "d'exemple Swagger UI non modifiées : remplacez-les par de "
                    "vrais labels (ex. {\"1\": 3.0}) ou supprimez le champ."
                ) from None
            if float(weight) < 0:
                raise ValueError(
                    f"Poids négatif interdit pour le label {label} : {weight}. "
                    "Les poids servent à normaliser des probabilités d'échantillonnage."
                )
            cleaned[str(label)] = float(weight)
        return cleaned


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