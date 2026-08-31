# project/core/pipeline_runner.py
"""Pipeline end-to-end : labeling (label_dataset) → filtrage par confidence →
fine-tuning LLM (finetune_llm).

Fonctions partagées par :
  - pipeline.py (CLI autonome, une commande pour tout enchaîner) ;
  - api/routes/pipeline.py (jobs asynchrones persistés, comme /train).

Le fine-tuning est lancé en subprocess (`python finetune_llm.py ...`) pour
isoler torch / les modèles chargés et réutiliser la CLI existante sans
refactor. Le labeling réutilise directement label_dataset.label_dataset(),
qui applique déjà le filtrage par confidence.
"""

import logging
import os
import subprocess
import sys
import threading
import time

from core.models import PipelineRequest, TrainJob, JobStatus
from core.job_store import get_job_store

logger = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Étapes exposées à l'UI (PipelineJobTracker du dashboard).
PIPELINE_STEPS = ["queued", "labeling", "filtering", "finetuning", "done"]

_job_cancel_events = {}


def get_cancel_event(job_id: str) -> threading.Event:
    return _job_cancel_events.setdefault(job_id, threading.Event())


def cancel_pipeline(job_id: str) -> TrainJob:
    store = get_job_store()
    job = store.get(job_id)
    if job is None:
        raise RuntimeError("job_id introuvable")

    get_cancel_event(job_id).set()

    job.status = JobStatus.CANCELLED
    job.step = "cancelled"
    job.error = "Pipeline cancelled by user"
    job.finished_at = time.time()
    store[job_id] = job
    logger.warning("Pipeline %s annulé par l'utilisateur", job_id)
    return job


def default_paths(job_id: str):
    """Chemins de sortie par défaut, isolés par job, sous experiments/pipeline/."""
    base = os.path.join("experiments", "pipeline", job_id)
    return (
        os.path.join(base, "labeled.jsonl"),
        os.path.join(base, "lora_model"),
    )


def run_labeling(params: PipelineRequest, labeled_output: str):
    """Étape 1 + 2 : labeling DistilBERT et filtrage par confidence.

    label_dataset.label_dataset() filtre déjà les prédictions sous
    ``min_confidence`` avant l'export JSONL Alpaca. Retourne la liste des
    records conservés (écrits dans ``labeled_output``).
    """
    from core.model_versioning import resolve_model_path
    from label_dataset import label_dataset

    model_dir = resolve_model_path(params.model_path)
    logger.info(
        "Labeling : input=%s output=%s model=%s min_confidence=%s batch_size=%s",
        params.input_path, labeled_output, model_dir,
        params.min_confidence, params.label_batch_size,
    )
    records = label_dataset(
        input_path=params.input_path,
        output_path=labeled_output,
        model_path=model_dir,
        text_column=params.text_column,
        min_confidence=params.min_confidence,
        batch_size=params.label_batch_size,
    )
    logger.info("%d record(s) conservé(s) par le filtrage confidence", len(records))
    return records


def build_finetune_cmd(params: PipelineRequest, train_file: str, output_dir: str):
    """Construit la commande subprocess pour finetune_llm.py.

    Les paramètres à None sont omis : finetune_llm.py applique ses défauts.
    """
    cmd = [
        sys.executable,
        os.path.join(PROJECT_ROOT, "finetune_llm.py"),
        "--train_file", train_file,
        "--output_dir", output_dir,
    ]
    optional = {
        "--base_model": params.base_model,
        "--validation_file": params.validation_file,
        "--epochs": params.epochs,
        "--batch_size": params.finetune_batch_size,
        "--gradient_accumulation_steps": params.gradient_accumulation_steps,
        "--learning_rate": params.learning_rate,
        "--max_seq_length": params.max_seq_length,
        "--lora_r": params.lora_r,
        "--lora_alpha": params.lora_alpha,
        "--lora_dropout": params.lora_dropout,
        "--target_modules": params.target_modules,
        "--seed": params.seed,
    }
    for flag, value in optional.items():
        if value is not None:
            cmd += [flag, str(value)]
    cmd.append("--use_qlora" if params.use_qlora else "--no_qlora")
    return cmd


def run_finetune(cmd, cancel_event: threading.Event = None, cwd: str = PROJECT_ROOT):
    """Exécute finetune_llm.py en subprocess, annulable via *cancel_event*.

    Lève RuntimeError sur code de sortie non nul et sur annulation.
    """
    logger.info("Finetuning lancé : %s", " ".join(cmd))
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        while True:
            ret = process.poll()
            if ret is not None:
                break
            if cancel_event is not None and cancel_event.wait(timeout=2.0):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                raise RuntimeError("Pipeline cancelled by user")
            time.sleep(0.5)
    except KeyboardInterrupt:
        process.terminate()
        raise

    output = process.stdout.read() if process.stdout else ""
    if ret != 0:
        tail = "\n".join(output.splitlines()[-20:]) if output else ""
        logger.error(
            "finetune_llm.py a échoué (code %s) :\n%s",
            ret, tail,
        )
        raise RuntimeError(
            f"finetune_llm.py a échoué (code {ret}).\n{tail}".rstrip()
        )
    if output:
        logger.debug("Sortie finetune_llm.py :\n%s", output)
    else:
        logger.debug("finetune_llm.py terminé (aucune sortie subprocess).")
    return output


def run_pipeline(job_id: str, req: PipelineRequest):
    """Thread target : exécute le pipeline complet et persiste le TrainJob.

    Étapes : labeling → filtering (garde-fou dataset vide) → finetuning.
    """
    store = get_job_store()
    job = store[job_id]
    cancel_event = get_cancel_event(job_id)

    job.status = JobStatus.RUNNING
    job.started_at = time.time()
    store[job_id] = job
    logger.info("Pipeline %s démarré | status=RUNNING", job_id)

    labeled_output, output_dir = req.labeled_output, req.output_dir
    if not labeled_output or not output_dir:
        def_labeled, def_dir = default_paths(job_id)
        labeled_output = labeled_output or def_labeled
        output_dir = output_dir or def_dir
        logger.info("Chemins par défaut pour %s : %s / %s", job_id, labeled_output, output_dir)

    try:
        if cancel_event.is_set():
            raise RuntimeError("Pipeline cancelled by user")

        job.step = "labeling"
        store[job_id] = job
        logger.info("Pipeline %s — étape %s", job_id, job.step)
        records = run_labeling(req, labeled_output)

        job.step = "filtering"
        job.model_path = labeled_output
        store[job_id] = job
        logger.info("Pipeline %s — étape %s (%d records)", job_id, job.step, len(records))

        # Garde-fou : inutile de lancer un fine-tuning sur un dataset vide.
        if not records:
            logger.warning(
                "Pipeline %s — dataset vide après filtrage confidence %s : "
                "finetuning non lancé",
                job_id, req.min_confidence,
            )
            raise RuntimeError(
                "Aucun record au-dessus du seuil de confidence "
                f"({req.min_confidence}) : le fine-tuning n'est pas lancé. "
                "Baissez min_confidence ou vérifiez le fichier d'entrée."
            )

        if cancel_event.is_set():
            raise RuntimeError("Pipeline cancelled by user")

        job.step = "finetuning"
        store[job_id] = job
        logger.info("Pipeline %s — étape %s", job_id, job.step)
        cmd = build_finetune_cmd(req, labeled_output, output_dir)
        run_finetune(cmd, cancel_event=cancel_event)

        job.model_path = output_dir
        job.status = JobStatus.COMPLETED
        job.step = "done"
        logger.info("Pipeline %s terminé | status=COMPLETED", job_id)

    except Exception as exc:
        job.status = JobStatus.FAILED
        job.error = str(exc)
        logger.error("Pipeline %s en échec | status=FAILED : %s", job_id, exc)
        if cancel_event.is_set():
            job.status = JobStatus.CANCELLED
            job.step = "cancelled"
            logger.warning("Pipeline %s annulé | status=CANCELLED", job_id)

    job.finished_at = time.time()
    store[job_id] = job

    base = os.path.join("experiments", "pipeline", job_id)
    return (
        os.path.join(base, "labeled.jsonl"),
        os.path.join(base, "lora_model"),
    )
