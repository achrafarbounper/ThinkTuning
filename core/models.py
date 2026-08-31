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
    # Back-translation FR→EN→FR via Helsinki-NLP/opus-mt : désactivée par
    # défaut (téléchargement de modèles + coût CPU/GPU au premier appel).
    use_back_translation: bool = True
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
                    "use_back_translation": False,
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


class EpochMetric(BaseModel):
    """Métriques d'entraînement pour une epoch (SCRUM-73)."""
    epoch: int
    loss: Optional[float] = None
    f1_macro: Optional[float] = None
    accuracy: Optional[float] = None


class TrainHistoryResponse(BaseModel):
    """Historique des métriques par epoch pour un job (GET /train/history/{job_id})."""
    job_id: str
    epochs: List[EpochMetric]


class ModelVersion(BaseModel):
    name: str
    path: str
    created_at: Optional[float] = None
    active: bool = False
class ModelVersion(BaseModel):
    name: str
    path: str
    created_at: Optional[float] = None
    active: bool = False


class PipelineRequest(BaseModel):
    """Paramètres du pipeline end-to-end : labeling (label_dataset) +
    filtrage par confidence + fine-tuning LLM (finetune_llm).

    Les champs optionnels à None signifient « utiliser la valeur par défaut
    du script cible » (aucun argument correspondant n'est transmis).
    """
    # --- Étape labeling + filtrage (label_dataset.py) ---
    input_path: str
    labeled_output: Optional[str] = None
    model_path: Optional[str] = None
    text_column: str = "text"
    min_confidence: float = 0.7
    label_batch_size: int = 32
    # --- Étape fine-tuning (finetune_llm.py) ---
    output_dir: Optional[str] = None
    base_model: Optional[str] = None
    validation_file: Optional[str] = None
    epochs: Optional[int] = None
    finetune_batch_size: Optional[int] = None
    gradient_accumulation_steps: Optional[int] = None
    learning_rate: Optional[float] = None
    max_seq_length: Optional[int] = None
    lora_r: Optional[int] = None
    lora_alpha: Optional[int] = None
    lora_dropout: Optional[float] = None
    target_modules: Optional[str] = None
    use_qlora: bool = True
    seed: int = 42