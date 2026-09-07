# project/core/trainer_runner.py

import time
import threading
import logging

from core.models import TrainJob, JobStatus, TRAIN_JOB_STEPS
from core.job_store import get_job_store
from core import job_logs
from core.model_versioning import (
    save_model_version,
    resolve_model_dir,
    MODEL_ROOT,
)
from src.dataset.loader import load_raw_dataset, augment_dataset
from src.dataset.preprocess import create_dataloaders
from src.model.distilbert import build_model
from src.model.trainer import Trainer, compute_class_weights
from src.utils.config import load_config
from transformers import AutoTokenizer, AutoModelForSequenceClassification
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


def load_source_f1(base_model_version: str):
    """Lit le F1 macro du training_report.json d'une version source.

    Garde-fou anti-régression : retourne le ``metrics.f1_macro`` de la version
    ``experiments/models/<version>``, ou None si le rapport est absent,
    illisible ou sans F1 (une version ancienne/incomplète ne doit jamais faire
    échouer le job).
    """
    import json
    import os

    report_path = os.path.join(MODEL_ROOT, base_model_version, "training_report.json")
    try:
        with open(report_path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
        f1 = (report.get("metrics") or {}).get("f1_macro")
        return float(f1) if f1 is not None else None
    except (OSError, ValueError, TypeError):
        logger.warning(
            "F1 source illisible pour la version %s (garde-fou anti-régression désactivé)",
            base_model_version,
        )
        return None


def check_regression(new_f1, source_f1, threshold: float = 0.0):
    """Compare le F1 de la nouvelle version à celui de la version source.

    Retourne un détail lisible si ``new_f1 < source_f1 - threshold``
    (régression), sinon None. Les F1 indisponibles (None) ne déclenchent
    jamais de régression.
    """
    if new_f1 is None or source_f1 is None:
        return None
    if float(new_f1) < float(source_f1) - threshold:
        return (
            f"F1 macro {float(new_f1):.4f} < F1 source {float(source_f1):.4f} "
            f"(seuil {threshold:.4f}) : la nouvelle version régresse par rapport "
            "à la version source. Envisagez de repartir de la version source."
        )
    return None


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


# ---------------------------------------------------------------------------
# Avancement temps réel (job.progress) : état par étape + pourcentage global
# ---------------------------------------------------------------------------

# Pondération du pourcentage global (toutes étapes confondues) :
#   étapes préparatoires (queued -> loading_model) : 20 %
#   training : 70 % · saving_model : 10 %
PREP_WEIGHT = 20.0
TRAIN_WEIGHT = 70.0
SAVE_WEIGHT = 10.0
# Part de l'epoch consommée par la phase eval (le reste va au train).
EVAL_FRACTION_OF_EPOCH = 0.3

_PREP_STEPS = TRAIN_JOB_STEPS[: TRAIN_JOB_STEPS.index("training")]


def _compute_global_pct(step, phase=None, epoch=None, epochs_total=None,
                        batch=None, batches_total=None) -> float:
    """Pourcentage global du pipeline (0-100), toutes étapes confondues."""
    if step == "done":
        return 100.0
    if step in _PREP_STEPS:
        # Chaque étape préparatoire consomme une part égale des 20 %.
        idx = _PREP_STEPS.index(step)
        return round(PREP_WEIGHT * idx / len(_PREP_STEPS), 1)
    if step == "training":
        if not (epoch and epochs_total and batches_total):
            return PREP_WEIGHT
        # Fraction de l'epoch consommée : 70 % train + 30 % eval.
        if phase == "train" and batch:
            frac = (1.0 - EVAL_FRACTION_OF_EPOCH) * min(batch, batches_total) / batches_total
        elif phase == "eval" and batch:
            frac = (1.0 - EVAL_FRACTION_OF_EPOCH) + EVAL_FRACTION_OF_EPOCH * min(
                batch, batches_total
            ) / batches_total
        else:
            frac = 0.0
        done_epochs = (epoch - 1) + frac
        return round(PREP_WEIGHT + TRAIN_WEIGHT * done_epochs / epochs_total, 1)
    if step == "saving_model":
        return round(PREP_WEIGHT + TRAIN_WEIGHT, 1)
    return 0.0


def _default_steps_dict() -> dict:
    return {s: {"status": "pending"} for s in TRAIN_JOB_STEPS}


def _set_step_progress(store, job_id: str, step: str):
    """Met à jour job.progress pour un changement d'étape du pipeline.

    Les étapes précédentes passent à ``done``, l'étape courante à ``active``,
    les suivantes restent ``pending``. Ne fait jamais échouer le job.
    """
    try:
        job = store.get(job_id)
        if job is None:
            return
        prog = dict(job.progress or {})
        steps = dict(prog.get("steps") or _default_steps_dict())
        try:
            idx = TRAIN_JOB_STEPS.index(step)
        except ValueError:
            idx = None
        for i, name in enumerate(TRAIN_JOB_STEPS):
            st = dict(steps.get(name) or {"status": "pending"})
            if idx is not None:
                if i < idx:
                    st["status"] = "done"
                elif i == idx:
                    st["status"] = "active"
            steps[name] = st
        prog["steps"] = steps
        prog["step"] = step
        prog["global_pct"] = _compute_global_pct(step)
        # Cas terminal : l'étape "done" elle-même est marquée done.
        if idx is not None and step == "done":
            steps["done"] = {"status": "done"}
        # Les infos batch ne concernent que l'étape "training" : on les purge
        # au changement d'étape (elles seront ré-ajoutées par on_progress).
        for key in ("phase", "batch", "batches_total", "batch_pct", "rate_it_s", "eta"):
            prog.pop(key, None)
        job.progress = prog
    except Exception:
        logger.exception("Échec de la mise à jour du progress (step) | job_id=%s", job_id)


def _update_batch_progress(store, job_id: str, info: dict):
    """Fusionne les infos batch-par-batch du callback ``on_progress`` du
    trainer dans job.progress (recalcule le pourcentage global)."""
    try:
        job = store.get(job_id)
        if job is None:
            return
        prog = dict(job.progress or {})
        prog.setdefault("steps", _default_steps_dict())
        prog.update(
            {
                "step": "training",
                "phase": info.get("phase"),
                "epoch": info.get("epoch"),
                "epochs_total": info.get("epochs_total"),
                "batch": info.get("step"),
                "batches_total": info.get("total"),
                "batch_pct": info.get("pct"),
                "rate_it_s": info.get("rate_it_s"),
                "eta": info.get("eta"),
            }
        )
        prog["global_pct"] = _compute_global_pct(
            "training", info.get("phase"), info.get("epoch"),
            info.get("epochs_total"), info.get("step"), info.get("total"),
        )
        job.progress = prog
    except Exception:
        logger.exception("Échec de la mise à jour du progress (batch) | job_id=%s", job_id)


def _mark_step_error(store, job_id: str):
    """Marque l'étape courante comme en erreur dans job.progress."""
    try:
        job = store.get(job_id)
        if job is None:
            return
        prog = dict(job.progress or {})
        steps = dict(prog.get("steps") or _default_steps_dict())
        current = prog.get("step")
        if current in steps:
            steps[current] = dict(steps[current] or {})
            steps[current]["status"] = "error"
            prog["steps"] = steps
            job.progress = prog
            store[job_id] = job
    except Exception:
        logger.exception("Échec du marquage d'erreur | job_id=%s", job_id)


def _set_step(job, store, job_id: str, step: str):
    """Transition d'étape canonique : job.step + progress + logs taggués."""
    job.step = step
    job_logs.set_job_log_step(step)
    _set_step_progress(store, job_id, step)
    store[job_id] = job


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

    # Capture des logs du thread courant pour ce job (WebSocket /train/stream).
    job_logs.ensure_job_log_handler()
    job_logs.attach_job_logging(job_id)
    job_logs.set_job_log_step("queued")

    job.status = JobStatus.RUNNING
    job.started_at = time.time()
    _set_step(job, store, job_id, "queued")

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

        _set_step(job, store, job_id, "loading_dataset")
        raw = load_raw_dataset(
            max_per_lang=req.max_per_lang,
            local_corrections_path=req.local_corrections_path,
        )
        logger.info(f"Dataset chargé | {len(raw)} exemples bruts")

        _set_step(job, store, job_id, "splitting_dataset")
        split = raw.train_test_split(test_size=0.1, seed=42)
        raw_train, raw_val = split["train"], split["test"]
        logger.debug(f"Split train/val : {len(raw_train)} / {len(raw_val)}")

        _set_step(job, store, job_id, "augmenting_dataset")
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

        _set_step(job, store, job_id, "building_dataloaders")
        train_loader, val_loader = create_dataloaders(augmented_train, raw_val, cfg)
        logger.debug(
            "Dataloaders construits | train batches=%s, val batches=%s",
            _safe_len(train_loader),
            _safe_len(val_loader),
        )

        _set_step(job, store, job_id, "computing_class_weights")
        class_weights = compute_class_weights(augmented_train["label"])
        try:
            weights_repr = class_weights.tolist()
        except AttributeError:
            weights_repr = "n/a"
        logger.debug("Poids de classe calculés : %s", weights_repr)

        _set_step(job, store, job_id, "loading_model")
        if TEST_MODE:
            from src.inference.tiny_tokenizer import TinyTokenizer
            from src.model.tiny_model import TinyModel

            tokenizer = TinyTokenizer()
            model = TinyModel()
            logger.debug(f"Modèle chargé en mode test : TinyModel sur {cfg['device']}")
        elif req.base_model_version:
            # Continual training : reprise des poids + tokenizer d'une version
            # précédente (experiments/models/<version>) au lieu du modèle de
            # base. Le reste du pipeline est inchangé : le résultat est une
            # nouvelle version chaînée sur l'ancienne (warm start).
            base_dir = resolve_model_dir(req.base_model_version)
            tokenizer = AutoTokenizer.from_pretrained(base_dir)
            model = AutoModelForSequenceClassification.from_pretrained(base_dir)
            logger.info(
                f"Continual training : reprise depuis la version "
                f"{req.base_model_version} -> {base_dir}"
            )
        else:
            tokenizer = AutoTokenizer.from_pretrained(cfg["model_name"])
            model = build_model(cfg)
            logger.info(f"Modèle chargé : {cfg['model_name']} sur {cfg['device']}")

        _set_step(job, store, job_id, "training")
        logger.debug(f"Configuration d'entraînement : {cfg}")
        trainer = Trainer(model, cfg, class_weights=class_weights)
        logger.info(f"Début de l'entraînement | {cfg['epochs']} epochs | device={cfg['device']}")

        # Diffusion temps réel (WebSocket /train/stream) : persister chaque
        # epoch dès qu'elle se termine (upsert ON CONFLICT(job_id, epoch)) au
        # lieu d'attendre la fin de l'entraînement. Le callback ne doit jamais
        # faire échouer le job : _persist_epoch_metrics journalise déjà les
        # erreurs sans les relancer.
        def _on_epoch_end(record):
            _persist_epoch_metrics(store, job_id, [record])

        # Avancement batch-par-batch (comme tqdm) -> job.progress -> WS.
        def _on_progress(info):
            _update_batch_progress(store, job_id, info)

        train_result = trainer.train(
            train_loader,
            val_loader,
            cancel_event=cancel_event,
            on_epoch_end=_on_epoch_end,
            on_progress=_on_progress,
        )
        if train_result is not None:
            logger.info(
                f"Entraînement terminé | early_stopped={train_result.get('early_stopped', False)}, "
                f"durée={train_result.get('training_duration_seconds')}s"
            )
            # SCRUM-73 : persistance des métriques par epoch dans le SQLite existant.
            _persist_epoch_metrics(store, job_id, train_result.get("epoch_metrics"))

        _set_step(job, store, job_id, "saving_model")
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

        # Garde-fou anti-régression (continual training) : comparer le F1
        # macro de la nouvelle version à celui de la version source. Ne fait
        # jamais échouer le job : on marque le job et on laisse la décision
        # (garder / repartir de la source) à l'opérateur.
        if req.base_model_version:
            new_f1 = (getattr(trainer, "final_metrics", None) or {}).get("f1_macro")
            source_f1 = load_source_f1(req.base_model_version)
            detail = check_regression(new_f1, source_f1)
            if detail:
                job.regression = True
                job.regression_detail = detail
                logger.warning(f"Régression détectée | job_id={job_id} | {detail}")
            else:
                logger.info(
                    "Garde-fou anti-régression OK | job_id=%s | f1=%s | f1_source=%s",
                    job_id,
                    new_f1,
                    source_f1,
                )

        job.model_path = model_dir
        job.status = JobStatus.COMPLETED
        _set_step(job, store, job_id, "done")
        logger.info(f"Entraînement terminé | job_id={job_id} | statut={job.status}")

    except Exception as exc:
        logger.exception("Échec du job d'entraînement")
        job.status = JobStatus.FAILED
        job.error = str(exc)
        # L'étape courante est marquée "error" dans job.progress (dashboard).
        _mark_step_error(store, job_id)
        # SCRUM-73 : si l'échec survient pendant l'entraînement, persister
        # les epochs déjà réalisées (si un trainer existe).
        _trainer = locals().get("trainer")
        if _trainer is not None:
            _persist_epoch_metrics(
                store, job_id, getattr(_trainer, "epoch_metrics", None) or []
            )

    job.finished_at = time.time()
    store[job_id] = job
    # Fin de la capture de logs : le thread ne doit plus tager de lignes.
    job_logs.detach_job_logging()
