# project/core/trainer_runner.py

import time
import threading

from core.models import TrainJob, JobStatus
from core.job_store import get_job_store
from core.model_versioning import save_model_version
from src.dataset.loader import load_raw_dataset, augment_dataset
from src.dataset.preprocess import create_dataloaders
from src.model.distilbert import build_model
from src.model.trainer import Trainer, compute_class_weights
from src.utils.config import load_config
from transformers import AutoTokenizer
from api import TEST_MODE
import torch
_job_cancel_events = {}


def get_cancel_event(job_id: str) -> threading.Event:
    return _job_cancel_events.setdefault(job_id, threading.Event())


def cancel_training(job_id: str):
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

    return job


def run_training(job_id: str, req):
    store = get_job_store()
    job = store[job_id]
    cancel_event = get_cancel_event(job_id)

    job.status = JobStatus.RUNNING
    job.started_at = time.time()
    store[job_id] = job

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

        job.step = "loading_dataset"
        store[job_id] = job
        raw = load_raw_dataset(max_per_lang=req.max_per_lang)

        job.step = "splitting_dataset"
        store[job_id] = job
        split = raw.train_test_split(test_size=0.1, seed=42)
        raw_train, raw_val = split["train"], split["test"]

        job.step = "augmenting_dataset"
        store[job_id] = job
        augmented_train = augment_dataset(
            raw_train,
            variants_per_example=req.variants_per_example,
            augment_fraction=req.augment_fraction,
            class_augment_weights=req.class_augment_weights,
        )

        job.step = "building_dataloaders"
        store[job_id] = job
        train_loader, val_loader = create_dataloaders(augmented_train, raw_val, cfg)

        job.step = "computing_class_weights"
        store[job_id] = job
        class_weights = compute_class_weights(augmented_train["label"])

        job.step = "loading_model"
        store[job_id] = job
        print(TEST_MODE)
        if TEST_MODE:
            from src.inference.tiny_tokenizer import TinyTokenizer
            from src.model.tiny_model import TinyModel

            tokenizer = TinyTokenizer()
            model = TinyModel()
        else:
            tokenizer = AutoTokenizer.from_pretrained(cfg["model_name"])
            model = build_model(cfg)

        job.step = "training"
        store[job_id] = job
        print(cfg)
        trainer = Trainer(model, cfg, class_weights=class_weights)
        trainer.train(train_loader, val_loader, cancel_event=cancel_event)

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

        job.model_path = model_dir
        job.status = JobStatus.COMPLETED
        job.step = "done"

    except Exception as exc:
        import traceback
        traceback.print_exc()  # ← affiche l’erreur réelle dans la console pytest
        job.status = JobStatus.FAILED
        job.error = str(exc)

    job.finished_at = time.time()
    store[job_id] = job
