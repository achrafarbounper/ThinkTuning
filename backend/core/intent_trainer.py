# project/core/intent_trainer.py

"""Runner d'entraînement du classifieur d'intention (chat/action) — SCRUM-95.

Refactor du script ``scripts/train_intent.py`` en module importable : la
logique d'entraînement est exécutée dans un thread daemon par la route
``POST /train/intent`` (api/routes/intent_train.py) et suit le même contrat de
job que l'entraînement sentiment (``core/trainer_runner.py``) :

  - job persisté dans le store partagé (core/job_store.py) avec ``kind="intent"`` ;
  - étapes canoniques ``INTENT_TRAIN_JOB_STEPS`` reflétées dans ``job.step`` et
    ``job.progress`` (même structure que le sentiment) ;
  - métriques par epoch persistées dans la table ``train_metrics`` existante
    (le WebSocket /train/stream les diffuse sans changement) ;
  - logs du thread capturés par core/job_logs.py (événements ``log``) ;
  - annulation coopérative : un ``threading.Event`` par job, vérifié entre les
    étapes et à chaque batch via un callback HF ``TrainerCallback``.

Différences assumées avec l'entraînement sentiment :
  - dataset JSONL local ``{"text", "label"}`` (pas de dataset HF, pas d'EDA) ;
  - encodeur ``AutoModelForSequenceClassification`` entraîné avec le ``Trainer``
    Hugging Face (pas le ``Trainer`` maison de src/model/trainer.py) ;
  - versions dans ``experiments/intent_models/<horodatage>`` via
    core/intent_store.py (activation = pointeur ``active.json``) ;
  - métriques : accuracy + confiance moyenne (pas de F1 macro 3 classes).

Note d'implémentation : contrairement à ``trainer_runner.run_training`` dont
l'``except Exception`` écrase le statut CANCELLED posé par ``cancel_training``,
ce module attrape explicitement ``IntentTrainingCancelled`` AVANT
``Exception`` afin de conserver le statut « cancelled » (contrat attendu par
le dashboard et par POST /train/intent/cancel/{job_id}).

Les imports lourds (torch / transformers / datasets) sont faits DANS le thread
du job, à l'étape ``loading_model`` : importer ce module reste léger (tests,
import de l'API), conformément à la règle du projet sur la couche classifiers.
"""

from __future__ import annotations

import datetime
import json
import logging
import math
import random
import threading
import time
from pathlib import Path

from core import job_logs
from core.intent_store import (
    INTENT_MODEL_ROOT,
    default_intent_labels,
    list_intent_model_versions,
    resolve_intent_model_dir,
    set_active_intent_version,
)
from core.job_store import get_job_store
from core.models import (
    INTENT_TRAIN_JOB_STEPS,
    IntentTrainRequest,
    JobStatus,
    TrainJob,
)

logger = logging.getLogger(__name__)

# Pondération du pourcentage global : préparation 20 %, entraînement 70 %,
# sauvegarde 10 % (même répartition que l'entraînement sentiment).
_PREP_WEIGHT = 20.0
_TRAIN_WEIGHT = 70.0

# Pourcentage global de référence par étape (l'entraînement progresse ensuite
# de _PREP_WEIGHT à _PREP_WEIGHT + _TRAIN_WEIGHT selon l'avancement HF Trainer).
_STEP_BASE_PCT = {
    "queued": 0.0,
    "loading_dataset": _PREP_WEIGHT * 0.25,
    "splitting_dataset": _PREP_WEIGHT * 0.5,
    "loading_model": _PREP_WEIGHT,
    "training": _PREP_WEIGHT,
    "saving_model": _PREP_WEIGHT + _TRAIN_WEIGHT,
    "done": 100.0,
}

# Registre des Events d'annulation par job (même pattern que trainer_runner).
_intent_cancel_events: dict = {}


class IntentTrainingCancelled(RuntimeError):
    """Levée quand l'Event d'annulation est activé pendant l'entraînement."""


def get_intent_cancel_event(job_id: str) -> threading.Event:
    """Event d'annulation du job (source unique partagée route/runner)."""
    return _intent_cancel_events.setdefault(job_id, threading.Event())


def cancel_intent_training(job_id: str) -> TrainJob:
    """Annule le job d'intention *job_id* (POST /train/intent/cancel/{job_id}).

    Pose immédiatement le statut CANCELLED dans le store et active l'Event
    que le callback HF Trainer vérifie batch par batch. RuntimeError si le
    job est introuvable (la route répond alors 404).
    """
    store = get_job_store()
    job = store.get(job_id)
    if job is None:
        raise RuntimeError("job_id introuvable")

    event = get_intent_cancel_event(job_id)
    event.set()

    job.status = JobStatus.CANCELLED
    job.step = "cancelled"
    job.error = "Intent training cancelled by user"
    job.finished_at = time.time()
    store[job_id] = job

    logger.info("Entraînement d'intention annulé | job_id=%s", job_id)
    return job


# ---------------------------------------------------------------------------
# Helpers dataset / versions (extraits de scripts/train_intent.py)
# ---------------------------------------------------------------------------

def _load_records(dataset_path: Path) -> list:
    """Charge un dataset JSONL ``{"text", "label"}`` (une ligne = un exemple)."""
    records: list = []
    with open(dataset_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            records.append({"text": str(record["text"]), "label": str(record["label"])})
    return records


def _save_model(model, tokenizer, output_dir: Path) -> None:
    """Sauvegarde model + tokenizer dans *output_dir* (identique au CLI)."""
    output_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(output_dir))
    tokenizer.save_pretrained(str(output_dir))
    logger.info("Version sauvegardée : %s", output_dir)


def _new_version_name(job_id: str) -> str:
    """Nom de version horodaté (même format que le CLI : %Y%m%dT%H%M%SZ).

    Anti-collision : si un dossier identique existe déjà (deux lancements la
    même seconde), un suffixe court dérivé du job_id est ajouté — le runner
    API ne doit jamais écraser une version existante.
    """
    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    if (Path(INTENT_MODEL_ROOT) / timestamp).exists():
        return f"{timestamp}-{job_id[:8]}"
    return timestamp


def _split_records(records: list, test_size: float, seed: int = 42):
    """Split déterministe train/val sur les exemples bruts (graine fixe 42).

    Le CLI historique splittait APRÈS tokenisation via
    ``Dataset.train_test_split(test_size=0.1, seed=42)`` ; splitter les
    exemples bruts avant est équivalent (la tokenisation est déterministe)
    et permet d'exposer l'étape ``splitting_dataset`` avant le chargement du
    modèle. Avec un seul exemple, pas de split (val vide → évaluation
    désactivée en aval).
    """
    indices = list(range(len(records)))
    random.Random(seed).shuffle(indices)
    n_val = max(1, round(len(indices) * test_size)) if len(indices) > 1 else 0
    val_idx = set(indices[:n_val])
    train_records = [records[i] for i in indices if i not in val_idx]
    val_records = [records[i] for i in indices if i in val_idx]
    return train_records, val_records


# ---------------------------------------------------------------------------
# Avancement temps réel (job.progress) — même structure que trainer_runner
# ---------------------------------------------------------------------------

def _default_steps_dict() -> dict:
    """État initial des étapes canoniques dans job.progress["steps"]."""
    return {step: {"status": "pending"} for step in INTENT_TRAIN_JOB_STEPS}


def _set_step_progress(store, job_id: str, step: str) -> None:
    """Met à jour job.progress pour la nouvelle étape courante (défensif)."""
    try:
        job = store.get(job_id)
        if job is None:
            return
        prog = dict(job.progress or {})
        steps = dict(prog.get("steps") or _default_steps_dict())
        current_index = INTENT_TRAIN_JOB_STEPS.index(step)
        for name in steps:
            state = dict(steps[name] or {})
            name_index = INTENT_TRAIN_JOB_STEPS.index(name)
            if name_index < current_index and state.get("status") != "error":
                state["status"] = "done"
            elif name == step:
                state["status"] = "active"
            elif name_index > current_index:
                state["status"] = "pending"
            steps[name] = state
        prog["steps"] = steps
        prog["step"] = step
        prog["global_pct"] = _STEP_BASE_PCT.get(step, prog.get("global_pct", 0.0))
        job.progress = prog
        store[job_id] = job
    except Exception:
        logger.exception("Échec de la mise à jour du progress (step) | job_id=%s", job_id)


def _update_train_progress(store, job_id: str, state) -> None:
    """Fusionne l'avancement HF Trainer (state.epoch / num_train_epochs)."""
    try:
        job = store.get(job_id)
        if job is None or state is None:
            return
        epochs_total = float(getattr(state, "num_train_epochs", 0) or 0)
        epoch = float(getattr(state, "epoch", 0) or 0)
        if epochs_total <= 0:
            return
        frac = min(1.0, max(0.0, epoch / epochs_total))
        prog = dict(job.progress or {})
        prog.setdefault("steps", _default_steps_dict())
        prog.update(
            {
                "step": "training",
                "phase": "train",
                "epoch": min(int(epochs_total), max(1, math.ceil(epoch - 1e-9))),
                "epochs_total": int(epochs_total),
                "global_pct": round(_PREP_WEIGHT + _TRAIN_WEIGHT * frac, 1),
            }
        )
        job.progress = prog
        store[job_id] = job
    except Exception:
        logger.exception("Échec de la mise à jour du progress (train) | job_id=%s", job_id)


def _mark_step_error(store, job_id: str) -> None:
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


def _persist_epoch_metrics(store, job_id: str, records) -> None:
    """Persiste les métriques par epoch (table train_metrics partagée).

    Ne doit jamais faire échouer le job : les erreurs sont seulement
    journalisées (même contrat que trainer_runner._persist_epoch_metrics).
    """
    if not records:
        return
    try:
        store.save_epoch_metrics(job_id, records)
        logger.info(
            "Métriques par epoch persistées | job_id=%s | %d epoch(s)",
            job_id,
            len(records),
        )
    except Exception:
        logger.exception(
            "Échec de la persistance des métriques par epoch | job_id=%s", job_id
        )


def _set_step(job, store, job_id: str, step: str) -> None:
    """Transition d'étape canonique : job.step + progress + logs taggués."""
    job.step = step
    job_logs.set_job_log_step(step)
    _set_step_progress(store, job_id, step)
    store[job_id] = job


# ---------------------------------------------------------------------------
# Runner (Command exécuté dans un thread daemon par la route)
# ---------------------------------------------------------------------------

def run_intent_training(job_id: str, req: IntentTrainRequest) -> None:
    """Exécute l'entraînement d'intention pour le job *job_id*.

    Bloquant : à exécuter dans un thread daemon dédié (même pattern que
    ``trainer_runner.run_training``). Ne lève jamais : le résultat est reflété
    dans le store (completed / failed / cancelled) pour le suivi par le
    dashboard via GET /train/intent/status/{job_id}.
    """
    store = get_job_store()
    job = store[job_id]
    cancel_event = get_intent_cancel_event(job_id)

    # Capture des logs du thread courant pour ce job (WebSocket /train/stream).
    job_logs.ensure_job_log_handler()
    job_logs.attach_job_logging(job_id)
    job_logs.set_job_log_step("queued")

    job.status = JobStatus.RUNNING
    job.started_at = time.time()
    _set_step(job, store, job_id, "queued")

    logger.info(
        "Démarrage de l'entraînement d'intention | job_id=%s | base=%s",
        job_id,
        req.base_model,
    )

    try:
        _run_intent_pipeline(job, store, job_id, req, cancel_event)
        job.status = JobStatus.COMPLETED
        _set_step(job, store, job_id, "done")
        logger.info(
            "Entraînement d'intention terminé | job_id=%s | statut=%s",
            job_id,
            job.status,
        )
    except IntentTrainingCancelled:
        # Annulation : on CONSERVE le statut CANCELLED posé par
        # cancel_intent_training — l'except Exception ci-dessous ne doit
        # pas le réécrire en "failed".
        job.status = JobStatus.CANCELLED
        job.error = "Intent training cancelled by user"
        logger.info(
            "Entraînement d'intention annulé en cours de route | job_id=%s",
            job_id,
        )
    except Exception as exc:
        logger.exception("Échec du job d'entraînement d'intention")
        job.status = JobStatus.FAILED
        job.error = str(exc)
        # L'étape courante est marquée "error" dans job.progress (dashboard).
        _mark_step_error(store, job_id)

    job.finished_at = time.time()
    store[job_id] = job
    # Fin de la capture de logs : le thread ne doit plus tager de lignes.
    job_logs.detach_job_logging()


def _run_intent_pipeline(job, store, job_id: str, req, cancel_event) -> None:
    """Corps de l'entraînement (étapes canoniques), appelé par run_intent_training.

    Lève ``IntentTrainingCancelled`` si l'Event d'annulation est activé, et
    toute autre exception en cas d'échec (gérées par run_intent_training).
    """
    labels = list(default_intent_labels())

    def _check_cancelled() -> None:
        if cancel_event.is_set():
            raise IntentTrainingCancelled()

    # 1) Dataset JSONL {"text", "label"} --------------------------------------
    _check_cancelled()
    _set_step(job, store, job_id, "loading_dataset")
    records = _load_records(Path(req.dataset_path))
    if not records:
        raise ValueError("Dataset vide.")
    unknown = sorted({r["label"] for r in records} - set(labels))
    if unknown:
        raise ValueError(
            f"Labels inconnus dans le dataset : {unknown} (attendus : {labels})"
        )
    counts = {label: sum(1 for r in records if r["label"] == label) for label in labels}
    logger.info("Dataset chargé : %d lignes (%s)", len(records), counts)

    # 2) Split déterministe train/val -----------------------------------------
    _check_cancelled()
    _set_step(job, store, job_id, "splitting_dataset")
    train_records, val_records = _split_records(records, req.test_size)
    logger.info("Split train/val : %d / %d", len(train_records), len(val_records))

    # 3) Modèle (imports lourds confinés au thread de job) ---------------------
    _check_cancelled()
    _set_step(job, store, job_id, "loading_model")
    try:
        import torch  # noqa: F401 (vérifie la disponibilité)
        from datasets import Dataset
        from transformers import (
            AutoModelForSequenceClassification,
            AutoTokenizer,
            Trainer,
            TrainerCallback,
            TrainingArguments,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Dépendances d'entraînement absentes : torch / transformers / "
            "datasets. Installez-les ou utilisez scripts/train_intent.py."
        ) from exc

    if req.base_model_version:
        # Continual training : reprise des poids + tokenizer d'une version
        # d'intention existante (validée tôt par la route ; revalidée ici).
        base_dir = resolve_intent_model_dir(req.base_model_version)
        tokenizer = AutoTokenizer.from_pretrained(base_dir, use_fast=False)
        model = AutoModelForSequenceClassification.from_pretrained(
            base_dir, num_labels=len(labels)
        )
        logger.info(
            "Continual training d'intention : reprise depuis la version %s -> %s",
            req.base_model_version,
            base_dir,
        )
    else:
        tokenizer = AutoTokenizer.from_pretrained(req.base_model, use_fast=False)
        model = AutoModelForSequenceClassification.from_pretrained(
            req.base_model, num_labels=len(labels)
        )
        logger.info("Modèle d'intention chargé : %s", req.base_model)

    # 4) Entraînement (Trainer HF) ---------------------------------------------
    _check_cancelled()
    _set_step(job, store, job_id, "training")
    version = _new_version_name(job_id)
    output_dir = Path(INTENT_MODEL_ROOT) / version

    def _to_label_id(label: str) -> int:
        return labels.index(label)

    def _tokenize(batch):
        return tokenizer(
            batch["text"],
            padding="max_length",
            truncation=True,
            max_length=req.max_length,
        )

    train_ds = Dataset.from_list(
        [{"text": r["text"], "labels": _to_label_id(r["label"])} for r in train_records]
    ).map(_tokenize, batched=True)
    eval_ds = None
    if val_records:
        eval_ds = Dataset.from_list(
            [{"text": r["text"], "labels": _to_label_id(r["label"])} for r in val_records]
        ).map(_tokenize, batched=True)

    def _compute_metrics(eval_pred):
        """Accuracy + finesse des probabilités (identique au CLI).

        ``eval_avg_confidence`` proche de 0.5 signale un modèle qui hésite ;
        ``eval_below_60pct`` est la part de prédictions rendues avec moins
        de 60 % de confiance.
        """
        import numpy as np

        logits = np.asarray(getattr(eval_pred, "predictions", eval_pred[0]))
        labels_true = np.asarray(getattr(eval_pred, "label_ids", eval_pred[1]))
        exp = np.exp(logits - logits.max(axis=-1, keepdims=True))
        probs = exp / exp.sum(axis=-1, keepdims=True)
        preds = probs.argmax(axis=-1)
        top = probs[np.arange(preds.shape[0]), preds]
        return {
            "eval_accuracy": float((preds == labels_true).mean()),
            "eval_avg_confidence": float(top.mean()),
            "eval_below_60pct": float((top < 0.6).mean()),
        }

    class _IntentJobCallback(TrainerCallback):
        """Annulation coopérative + avancement + métriques par epoch.

        Lever une exception dans un callback arrête la boucle HF Trainer ;
        ``IntentTrainingCancelled`` est interceptée par run_intent_training
        (le statut cancelled est conservé).
        """

        def __init__(self, event: threading.Event):
            self.event = event

        def on_step_end(self, args, state, control, **kwargs):
            if self.event.is_set():
                raise IntentTrainingCancelled()
            _update_train_progress(store, job_id, state)

        def on_evaluate(self, args, state, control, metrics=None, **kwargs):
            if self.event.is_set():
                raise IntentTrainingCancelled()
            if not metrics:
                return
            # Persistance immédiate (upsert) : le WebSocket /train/stream
            # diffuse l'epoch dès qu'elle est évaluée, comme pour le sentiment.
            epoch = int(round(float(metrics.get("epoch", 0) or 0)))
            _persist_epoch_metrics(
                store,
                job_id,
                [
                    {
                        "epoch": epoch,
                        "loss": metrics.get("eval_loss"),
                        "f1_macro": None,
                        "accuracy": metrics.get("eval_accuracy"),
                    }
                ],
            )
            logger.info(
                "Epoch %d évaluée | accuracy=%.3f | confiance moyenne=%.3f",
                epoch,
                float(metrics.get("eval_accuracy", 0.0)),
                float(metrics.get("eval_avg_confidence", 0.0)),
            )

    training_args = TrainingArguments(
        output_dir=str(output_dir),
        num_train_epochs=req.epochs,
        per_device_train_batch_size=req.batch_size,
        learning_rate=req.learning_rate,
        eval_strategy="epoch" if eval_ds is not None else "no",
        logging_strategy="steps",
        logging_steps=20,
        seed=42,
        report_to=[],
        save_strategy="no",  # une seule version finale, sauvegardée ci-dessous
        disable_tqdm=True,   # pas de barres tqdm dans les logs serveur
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_ds,
        eval_dataset=eval_ds,
        compute_metrics=_compute_metrics if eval_ds is not None else None,
        callbacks=[_IntentJobCallback(cancel_event)],
    )
    logger.info(
        "Début de l'entraînement | %d epochs | batch_size=%d | lr=%s",
        req.epochs,
        req.batch_size,
        req.learning_rate,
    )
    trainer.train()
    if eval_ds is not None:
        eval_metrics = trainer.evaluate()
        logger.info(
            "Évaluation finale : accuracy=%.3f, confiance moyenne=%.3f "
            "(%.1f%% des prédictions sous 60 %% de confiance)",
            float(eval_metrics.get("eval_accuracy", 0.0)),
            float(eval_metrics.get("eval_avg_confidence", 0.0)),
            float(eval_metrics.get("eval_below_60pct", 0.0)) * 100.0,
        )

    # 5) Quantification optionnelle (parité avec le CLI) ------------------------
    if req.quantize_int8:
        try:
            import torch.quantization as quant

            model = quant.quantize_dynamic(model, dtype=torch.qint8)
            logger.info("Quantification dynamique INT8 appliquée.")
        except Exception as exc:  # pragma: no cover - matériel/dépendances
            logger.warning(
                "Quantisation INT8 indisponible (%s) ; modèle FP32 conservé.", exc
            )

    # 6) Sauvegarde de la version + activation conditionnelle -------------------
    _check_cancelled()
    _set_step(job, store, job_id, "saving_model")
    _save_model(model, tokenizer, output_dir)
    logger.info("Versions disponibles : %s", list_intent_model_versions())

    job.model_path = str(output_dir)

    if req.activate:
        set_active_intent_version(version)
        logger.info("Version d'intention active : %s", version)