"""Tests de l'avancement temps réel des jobs d'entraînement :

  - core/job_logs : capture des logs par job (mapping thread -> job),
  - Trainer.on_progress : callback batch-par-batch des phases train/eval,
  - core/trainer_runner : état des étapes + pourcentage global (job.progress),
  - SQLitePollingEventsSource : get_progress / get_logs.
"""
import logging
import os
import tempfile

os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault(
    "JOB_STORE_PATH",
    os.path.join(tempfile.gettempdir(), "thinktuning-test-jobs-progress.db"),
)

import pytest
import torch
from torch import nn

import api  # noqa: E402,F401  (initialise l'app comme les autres tests)
from core import job_logs, trainer_runner
from core.job_store import get_job_store
from core.models import TRAIN_JOB_STEPS, JobStatus, TrainJob
from core.training_events import SQLitePollingEventsSource
from src.model.trainer import Trainer

# ---------------------------------------------------------------------------
# core/job_logs
# ---------------------------------------------------------------------------

def test_job_logs_capture_and_since_seq():
    job_id = "logs-job-1"
    job_logs.reset_job_logs(job_id)
    job_logs.ensure_job_log_handler()

    def worker():
        job_logs.attach_job_logging(job_id)
        try:
            job_logs.set_job_log_step("training")
            logging.getLogger("test").info("ligne A")
            job_logs.set_job_log_step("saving_model")
            logging.getLogger("test").warning("ligne B")
            logging.getLogger("test").debug("ligne DEBUG non capturée")
        finally:
            job_logs.detach_job_logging()

    worker()

    logs = job_logs.get_logs(job_id)
    # Le DEBUG est filtré (CAPTURE_LEVEL=INFO).
    messages = [l["message"] for l in logs]
    assert "ligne A" in messages
    assert "ligne B" in messages
    assert all("DEBUG" not in l["message"] for l in logs)
    # Tague d'étape + ordre des seq.
    assert logs[0]["step"] == "training"
    assert [l["seq"] for l in logs] == sorted(l["seq"] for l in logs)
    # Pagination par since_seq.
    last_seq = logs[-1]["seq"]
    assert job_logs.get_logs(job_id, since_seq=last_seq) == []
    assert [l["message"] for l in job_logs.get_logs(job_id, since_seq=logs[0]["seq"])] == [
        "ligne B"
    ]

    # Un thread détaché ne capture plus rien pour ce job.
    logging.getLogger("test").info("hors job")
    assert all("hors job" not in l["message"] for l in job_logs.get_logs(job_id))

    job_logs.reset_job_logs(job_id)
    assert job_logs.get_logs(job_id) == []


# ---------------------------------------------------------------------------
# Trainer.on_progress
# ---------------------------------------------------------------------------

class _DummyModel(nn.Module):
    """Mini-modèle avec la même signature d'appel que les modèles HF."""

    def __init__(self):
        super().__init__()
        self.emb = nn.Embedding(10, 8)
        self.fc = nn.Linear(8, 3)

    def forward(self, input_ids, attention_mask=None):
        import types

        logits = self.fc(self.emb(input_ids).mean(dim=1))
        return types.SimpleNamespace(logits=logits)


def _make_batch():
    return {
        "input_ids": torch.randint(0, 10, (2, 4)),
        "attention_mask": torch.ones(2, 4, dtype=torch.long),
        "labels": torch.tensor([0, 1]),
    }


CFG = {
    "device": "cpu",
    "torch_threads": 1,
    "learning_rate": 1e-3,
    "weight_decay": 0.0,
    "epochs": 2,
    "warmup_ratio": 0.0,
    "gradient_clip": 1.0,
}


def test_trainer_on_progress_train_and_eval():
    train_loader = [_make_batch(), _make_batch()]
    val_loader = [_make_batch()]
    trainer = Trainer(_DummyModel(), CFG)
    events = []
    trainer.train(train_loader, val_loader, on_progress=events.append)

    phases = [e["phase"] for e in events]
    assert "train" in phases and "eval" in phases
    train_events = [e for e in events if e["phase"] == "train"]
    eval_events = [e for e in events if e["phase"] == "eval"]
    # Dernier batch de chaque phase = total (le throttle saute les batches
    # intermédiaires mais le dernier est toujours envoyé).
    assert train_events[-1]["step"] == 2 and train_events[-1]["total"] == 2
    assert train_events[-1]["pct"] == pytest.approx(100.0)
    assert eval_events[-1]["step"] == 1 and eval_events[-1]["total"] == 1
    # Champs métriques cohérents avec une ligne tqdm.
    ev = train_events[-1]
    assert ev["epoch"] in (1, 2) and ev["epochs_total"] == 2
    assert ev["rate_it_s"] > 0 and ev["eta"] == pytest.approx(0.0)
    assert ev["elapsed"] >= 0


def test_trainer_on_progress_exception_swallowed():
    """Un callback qui lève ne doit jamais casser l'entraînement."""

    def boom(_info):
        raise RuntimeError("callback crash")

    trainer = Trainer(_DummyModel(), CFG)
    result = trainer.train([_make_batch()], [_make_batch()], on_progress=boom)
    assert result is not None and len(result["epoch_metrics"]) == 2


# ---------------------------------------------------------------------------
# core/trainer_runner : étapes + pourcentage global
# ---------------------------------------------------------------------------

def test_compute_global_pct():
    f = trainer_runner._compute_global_pct
    assert f("queued") == pytest.approx(0.0)
    assert f("loading_dataset") > 0
    assert f("loading_dataset") < f("loading_model")
    assert f("loading_model") < trainer_runner.PREP_WEIGHT
    # training sans info batch = base du segment
    assert f("training") == pytest.approx(trainer_runner.PREP_WEIGHT)
    # epoch 1/2, train batch 1/2 -> 20 + 70 * (0.35/2) (arrondi 1 décimale)
    expected = round(20 + 70 * (0.7 * 0.5) / 2, 1)
    assert f("training", "train", 1, 2, 1, 2) == pytest.approx(expected)
    # phase eval : 70 % de l'epoch consommé + moitié des 30 % d'eval
    expected_eval = round(20 + 70 * (0.7 + 0.3 * 0.5) / 2, 1)
    assert f("training", "eval", 1, 2, 1, 2) == pytest.approx(expected_eval)
    assert f("saving_model") == pytest.approx(90.0)
    assert f("done") == pytest.approx(100.0)


def test_set_step_progress_marks_statuses():
    store = get_job_store()
    job = TrainJob(job_id="progress-job-1", status=JobStatus.RUNNING)
    store[job.job_id] = job

    trainer_runner._set_step(job, store, job.job_id, "loading_dataset")
    prog = store[job.job_id].progress
    assert prog["step"] == "loading_dataset"
    assert prog["steps"]["queued"]["status"] == "done"
    assert prog["steps"]["loading_dataset"]["status"] == "active"
    assert prog["steps"]["training"]["status"] == "pending"
    assert prog["global_pct"] == pytest.approx(
        trainer_runner._compute_global_pct("loading_dataset")
    )

    trainer_runner._set_step(job, store, job.job_id, "done")
    prog = store[job.job_id].progress
    assert all(s["status"] == "done" for s in prog["steps"].values())
    assert prog["global_pct"] == pytest.approx(100.0)


def test_update_batch_progress_and_events_source():
    store = get_job_store()
    job = TrainJob(job_id="progress-job-2", status=JobStatus.RUNNING)
    store[job.job_id] = job
    trainer_runner._set_step(job, store, job.job_id, "training")

    trainer_runner._update_batch_progress(
        store,
        job.job_id,
        {
            "phase": "train",
            "epoch": 1,
            "epochs_total": 2,
            "step": 1,
            "total": 2,
            "pct": 50.0,
            "rate_it_s": 7.73,
            "elapsed": 7.7,
            "eta": 7.7,
        },
    )
    stored = store[job.job_id].progress
    assert stored["batch"] == 1 and stored["batches_total"] == 2
    assert stored["batch_pct"] == pytest.approx(50.0)
    assert stored["rate_it_s"] == pytest.approx(7.73)
    assert stored["eta"] == pytest.approx(7.7)
    assert 0 < stored["global_pct"] < 100

    # Source d'événements : get_progress / get_logs.
    source = SQLitePollingEventsSource()
    assert source.get_progress_sync(job.job_id) == stored
    assert source.get_progress_sync("job-inconnu") is None

    logging.getLogger("test").info("log du job progress-job-2")
    # Le thread courant n'est pas attaché à ce job : aucun log capturé.
    assert source.get_logs_sync(job.job_id) == []


def test_train_job_steps_order_matches_jobsteps_ts():
    """Garde-fou : TRAIN_JOB_STEPS (serveur) couvre les étapes affichées
    par le dashboard (TRAIN_STEPS de jobSteps.ts)."""
    from pathlib import Path

    js = (
        Path(__file__).resolve().parents[1]
        / "dashboard"
        / "src"
        / "api"
        / "jobSteps.ts"
    )
    content = js.read_text(encoding="utf-8")
    for step in TRAIN_JOB_STEPS:
        assert f'"{step}"' in content
