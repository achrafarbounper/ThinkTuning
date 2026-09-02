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
    # d'active learning (SCRUM-56 : merge_reviewed_data.py). ConcatÃ©nÃ© au
    # dataset HF avant split/augmentation. None => comportement inchangÃ©.
    local_corrections_path: Optional[str] = None
    # Continual training : nom d'une version existante dans experiments/models
    # (ex. "20260819T151459Z") à partir de laquelle reprendre l'entraînement
    # (poids + tokenizer) au lieu du modèle de base Hugging Face. None =>
    # comportement historique (from scratch sur le modèle de base).
    base_model_version: Optional[str] = None
    augment_fraction: float = 0.4
    variants_per_example: int = 2
    # Back-translation FRâ†’ENâ†’FR via Helsinki-NLP/opus-mt : dÃ©sactivÃ©e par
    # dÃ©faut (tÃ©lÃ©chargement de modÃ¨les + coÃ»t CPU/GPU au premier appel).
    use_back_translation: bool = False
    class_augment_weights: Optional[Dict[str, float]] = None
    epochs: Optional[int] = None
    batch_size: Optional[int] = None
    num_workers: Optional[int] = None
    max_length: Optional[int] = None
    learning_rate: Optional[float] = None
    weight_decay: Optional[float] = None
    warmup_ratio: Optional[float] = None
    device: str = "auto"
    # Exemple affichÃ©/prÃ©-rempli dans Swagger UI ("Try it out") : sans lui,
    # les dicts libres sont prÃ©-remplis avec des placeholders 'additionalProp1'
    # qui font planter l'entraÃ®nement s'ils sont envoyÃ©s tels quels.
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "max_per_lang": 500,

                    "local_corrections_path": None,
                    "base_model_version": None,
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
        """Rejette tÃ´t (HTTP 422) les clÃ©s non numÃ©riques et les poids négatifs.

        Les clÃ©s 'additionalProp1', 'additionalProp2'... sont les placeholders
        gÃ©nÃ©rÃ©s par Swagger UI pour les dicts libres : elles arrivaient jusque
        dans augment_dataset() et provoquaient un ValueError obscur en plein
        job d'entraÃ®nement au lieu d'une 422 immÃ©diate cÃ´tÃ© API.
        """
        if v is None:
            return v
        cleaned: Dict[str, float] = {}
        for key, weight in v.items():
            try:
                label = int(str(key).strip())
            except (TypeError, ValueError):
                raise ValueError(
                    f"ClÃ© invalide dans class_augment_weights : {key!r}. "
                    "Attendu un label de classe entier (ex. 0, 1, 2). Les clÃ©s "
                    "'additionalProp1', 'additionalProp2'... sont des valeurs "
                    "d'exemple Swagger UI non modifiÃ©es : remplacez-les par de "
                    "vrais labels (ex. {\"1\": 3.0}) ou supprimez le champ."
                ) from None
            if float(weight) < 0:
                raise ValueError(
                    f"Poids négatif interdit pour le label {label} : {weight}. "
                    "Les poids servent Ã  normaliser des probabilitÃ©s d'Ã©chantillonnage."
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
    # Garde-fou anti-régression (continual training) : True si le F1 macro
    # de la nouvelle version est inférieur à celui de la version source.
    regression: bool = False
    regression_detail: Optional[str] = None
    # Avancement temps réel (batch-par-batch + état des étapes du pipeline).
    # Dict sérialisé en JSON par le PersistentJobStore : aucune migration
    # SQLite nécessaire. Structure :
    #   {"step": "training",
    #    "steps": {<step>: {"status": "done|active|pending|error"}},
    #    "phase": "train|eval", "epoch": N, "epochs_total": N,
    #    "batch": N, "batches_total": N, "batch_pct": float,
    #    "global_pct": float, "rate_it_s": float, "eta": float}
    progress: Optional[Dict] = None


# Ordre canonique des étapes du pipeline d'entraînement (core/trainer_runner.py
# et dashboard/src/api/jobSteps.js — à garder alignés).
TRAIN_JOB_STEPS = [
    "queued",
    "loading_dataset",
    "splitting_dataset",
    "augmenting_dataset",
    "building_dataloaders",
    "computing_class_weights",
    "loading_model",
    "training",
    "saving_model",
    "done",
]

class JobListResponse(BaseModel):
    """RÃ©ponse paginÃ©e pour GET /train/jobs."""
    total: int
    items: List[TrainJob]
    limit: int
    offset: int


class EpochMetric(BaseModel):
    """MÃ©triques d'entraÃ®nement pour une epoch (SCRUM-73)."""
    epoch: int
    loss: Optional[float] = None
    f1_macro: Optional[float] = None
    accuracy: Optional[float] = None


class ScheduleRequest(BaseModel):
    """Corps de POST /train/schedule â€” planification d'un entraÃ®nement rÃ©current.

    Exactement l'un des deux champs ``cron`` ou ``interval_minutes`` doit Ãªtre
    fourni (sinon 422). ``train`` contient les paramÃ¨tres passÃ©s Ã  chaque
    exÃ©cution (identiques Ã  POST /train).
    """
    train: TrainRequest
    cron: Optional[str] = None              # ex. "0 2 * * *" (5 champs, cron standard)
    interval_minutes: Optional[int] = None  # ex. 60 : toutes les heures

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        fields = v.strip().split()
        if len(fields) != 5:
            raise ValueError(
                "cron doit contenir exactement 5 champs (min heure jour_du_mois "
                "mois jour_de_la_semaine), ex. '0 2 * * *'."
            )
        return " ".join(fields)

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "train": {"max_per_lang": 500, "epochs": 3, "device": "auto"},
                    "cron": "0 2 * * *",
                    "interval_minutes": None,
                }
            ]
        }
    )


class ScheduledJob(BaseModel):
    """Une planification d'entraÃ®nement rÃ©currente (SCRUM-34)."""
    schedule_id: str
    status: str = "scheduled"  # scheduled | removed
    trigger: str               # "cron" ou "interval"
    cron: Optional[str] = None
    interval_minutes: Optional[int] = None
    next_run_at: Optional[float] = None
    created_at: float
    train_request: TrainRequest


class ScheduleListResponse(BaseModel):
    """RÃ©ponse de GET /train/schedules."""
    total: int
    items: List[ScheduledJob]


class TrainHistoryResponse(BaseModel):
    """Historique des mÃ©triques par epoch pour un job (GET /train/history/{job_id})."""
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
    """ParamÃ¨tres du pipeline end-to-end : labeling (label_dataset) +
    filtrage par confidence + fine-tuning LLM (finetune_llm).

    Les champs optionnels Ã  None signifient Â« utiliser la valeur par dÃ©faut
    du script cible Â» (aucun argument correspondant n'est transmis).
    """
    # --- Ã‰tape labeling + filtrage (label_dataset.py) ---
    input_path: str
    labeled_output: Optional[str] = None
    model_path: Optional[str] = None
    text_column: str = "text"
    min_confidence: float = 0.7
    label_batch_size: int = 32
    # --- Ã‰tape fine-tuning (finetune_llm.py) ---
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


class CycleRequest(BaseModel):
    """Requete du cycle complet Active Learning -> annotation -> retrain -> activation (SCRUM-55).

    ``train`` : parametres d'entrainement optionnels (TrainRequest). Les champs
    ``local_corrections_path`` et ``base_model_version`` sont ecrases par le
    cycle (fusion des annotations / continual training depuis la version active).
    """
    auto_activate: bool = True
    train: Optional[TrainRequest] = None


class ActiveLearningRequest(BaseModel):
    """Requete POST /active_learning : selection d'exemples incertains.

    Fournir ``texts`` (liste explicite) et/ou ``dataset_path`` (JSONL/CSV avec
    colonne ``text``). Par defaut, le dataset enrichi data/train_enriched.jsonl.
    """
    texts: Optional[List[str]] = None
    dataset_path: Optional[str] = None
    top_n: Optional[int] = 50
    batch_size: int = 32
    model_version: Optional[str] = None


class AnnotateRequest(BaseModel):
    """Requete POST /annotate : correction manuelle d'un exemple."""
    text: str
    label: str  # negative / neutral / positive (alias FR acceptes)
    force: bool = False


class AnnotateListResponse(BaseModel):
    total: int
    items: List[dict]


class MergeAnnotationsResponse(BaseModel):
    stats: dict