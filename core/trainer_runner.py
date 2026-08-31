# project/core/trainer_runner.py

import time
import threading
import logging

from core.models import TrainJob, JobStatus
from core.job_store import get_job_store
from core.model_versioning import save_model_version
from src.dataset.loader import load_raw_dataset, augment_dataset
from src.dataset.preprocess import create_dataloaders
from src.model.distilbert import build_model
from src.model.trainer import Trainer, compute_class_weights
from src.utils.config import load_config
from transformers import AutoTokenizer
from src.utils.flags import TEST_MODE
import torch
logger = logging.getLogger(__name__)
_job_cancel_events = {}


def _safe_len(obj):
    """len() tolérant : renvoie 'n/a' si l'objet n'est pas mesurable."""
    try:
        return len(obj)
    except TypeError:
        return "n/a"


def _persist_epoch_metrics(store, job_id: str, records):
    """Persiste les métriques par epoch dans le SQLite existant (SCRUM-73).

    Ne doit jamais faire échouer le job d'entraînement : les erreurs sont
    seulement journalisées.
    """
    if not records:
        return
    try:
        store.save_epoch_metrics(job_id, records)
        logger.info(f"Métriques par epoch persistées | job_id={job_id} | {len(records)} epoch(s)")
    except Exception:
        logger.exception(
            "Échec de la persistance des métriques par epoch | job_id=%s", job_id
        )


def get_cancel_event(job_id: str) -> threading.Event:
    return _job_cancel_events.setdefault(job_id, threading.Event())


def cancel_training(job_id: str):
    logger.debug(f"cancel_training : début | job_id={job_id}")
    store = get_job_store()
    job = store.get(job_id)
    if job is None:
        raise RuntimeError("job_id introuvable")

    event = get_cancel_event(job_id)
    event.set()

    job.status = JobStatus.CANCELLED
    job.step = "cancelled"
    job.error = "Training cancelled by user"
    job.finished_at = time.time()
    store[job_id] = job

    logger.info(f"Entraînement annulé | job_id={job_id}")
    return job


def run_training(job_id: str, req):
    store = get_job_store()
    job = store[job_id]
    cancel_event = get_cancel_event(job_id)

    job.status = JobStatus.RUNNING
    job.started_at = time.time()
    store[job_id] = job

    logger.info(f"Démarrage de l'entraînement | job_id={job_id} | device={req.device}")

    try:
        # 1) Charger la config
        cfg = load_config("configs/default.yaml")

        # 2) Appliquer les paramètres utilisateur
        for key, value in req.model_dump().items():
            if value is not None:
                cfg[key] = value

        # 3) Device logique normale
        if req.device == "auto":
            cfg["device"] = "cuda" if torch.cuda.is_available() else "cpu"
        else:
            cfg["device"] = req.device

        # 4) Override en mode test
        if TEST_MODE:
            cfg["device"] = "cpu"

        logger.debug(f"Config chargée | device retenu={cfg['device']} | TEST_MODE={TEST_MODE}")

        job.step = "loading_dataset"
        store[job_id] = job
        raw = load_raw_dataset(
            max_per_lang=req.max_per_lang,
            local_corrections_path=req.local_corrections_path,
        )
        logger.info(f"Dataset chargé | {len(raw)} exemples bruts")

        job.step = "splitting_dataset"
        store[job_id] = job
        split = raw.train_test_split(test_size=0.1, seed=42)
        raw_train, raw_val = split["train"], split["test"]
        logger.debug(f"Split train/val : {len(raw_train)} / {len(raw_val)}")

        job.step = "augmenting_dataset"
        store[job_id] = job
        augmented_train = augment_dataset(
            raw_train,
            variants_per_example=req.variants_per_example,
            augment_fraction=req.augment_fraction,
            use_back_translation=req.use_back_translation,
            class_augment_weights=req.class_augment_weights,
        )
        logger.info(
            f"Augmentation EDA : {len(augmented_train)} exemples "
            f"(variants={req.variants_per_example}, fraction={req.augment_fraction}, "
            f"back_translation={req.use_back_translation})"
        )

        job.step = "building_dataloaders"
        store[job_id] = job
        train_loader, val_loader = create_dataloaders(augmented_train, raw_val, cfg)
        logger.debug(
            "Dataloaders construits | train batches=%s, val batches=%s",
            _safe_len(train_loader),
            _safe_len(val_loader),
        )

        job.step = "computing_class_weights"
        store[job_id] = job
        class_weights = compute_class_weights(augmented_train["label"])
        try:
            weights_repr = class_weights.tolist()
        except AttributeError:
            weights_repr = "n/a"
        logger.debug("Poids de classe calculés : %s", weights_repr)

        job.step = "loading_model"
        store[job_id] = job
        if TEST_MODE:
            from src.inference.tiny_tokenizer import TinyTokenizer
            from src.model.tiny_model import TinyModel

            tokenizer = TinyTokenizer()
            model = TinyModel()
            logger.debug(f"Modèle chargé en mode test : TinyModel sur {cfg['device']}")
        else:
            tokenizer = AutoTokenizer.from_pretrained(cfg["model_name"])
            model = build_model(cfg)
            logger.info(f"Modèle chargé : {cfg['model_name']} sur {cfg['device']}")

        job.step = "training"
        store[job_id] = job
        logger.debug(f"Configuration d'entraînement : {cfg}")
        trainer = Trainer(model, cfg, class_weights=class_weights)
        logger.info(f"Début de l'entraînement | {cfg['epochs']} epochs | device={cfg['device']}")
        train_result = trainer.train(train_loader, val_loader, cancel_event=cancel_event)
        if train_result is not None:
            logger.info(
                f"Entraînement terminé | early_stopped={train_result.get('early_stopped', False)}, "
                f"durée={train_result.get('training_duration_seconds')}s"
            )
            # SCRUM-73 : persistance des métriques par epoch dans le SQLite existant.
            _persist_epoch_metrics(store, job_id, train_result.get("epoch_metrics"))

        job.step = "saving_model"
        store[job_id] = job
        model_dir = save_model_version(
            tokenizer,
            trainer,
            job_id,
            len(augmented_train["label"]),
            len(raw_val["label"]),
            job.started_at,
            time.time(),
        )
        logger.info(f"Modèle sauvegardé -> {model_dir}")

        job.model_path = model_dir
        job.status = JobStatus.COMPLETED
        job.step = "done"
        logger.info(f"Entraînement terminé | job_id={job_id} | statut={job.status}")

    except Exception as exc:
        logger.exception("Échec du job d'entraînement")
        job.status = JobStatus.FAILED
        job.error = str(exc)
        # SCRUM-73 : si l'échec survient pendant l'entraînement, persister
        # les epochs déjà réalisées (si un trainer existe).
        _trainer = locals().get("trainer")
        if _trainer is not None:
            _persist_epoch_metrics(
                store, job_id, getattr(_trainer, "epoch_metrics", None) or []
            )

    job.finished_at = time.time()
    store[job_id] = job
