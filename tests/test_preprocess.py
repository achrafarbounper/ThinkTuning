import gc
import os
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import torch
from datasets import Dataset
from transformers import AutoTokenizer

import api
from api import JobStatus, TrainJob, TrainRequest, _jobs, _job_cancel_events, _run_training, cancel_training
from evaluate import evaluate
from src.dataset.preprocess import tokenize_dataset, create_dataloaders

api.TEST_MODE = True

def test_tokenize_dataset_returns_tokenized_dataset():
    data = {"text": ["Bonjour le monde!", "Hello world!"], "label": [0, 2]}
    dataset = Dataset.from_dict(data)

    tokenized = tokenize_dataset(dataset, "distilbert-base-multilingual-cased", max_length=16)

    assert "input_ids" in tokenized.column_names
    assert "attention_mask" in tokenized.column_names
    assert "labels" in tokenized.column_names
    assert len(tokenized) == 2


def test_create_dataloaders_returns_valid_loaders():
    data = {"text": ["Bonjour le monde!", "Hello world!", "Coucou !"], "label": [0, 1, 2]}
    dataset = Dataset.from_dict(data)
    split = dataset.train_test_split(test_size=1/3, seed=42)
    train_ds, val_ds = split["train"], split["test"]
    cfg = {"model_name": "distilbert-base-multilingual-cased", "batch_size": 2, "device": "cpu", "num_workers": 0}

    train_loader, val_loader = create_dataloaders(train_ds, val_ds, cfg)

    assert len(train_loader.dataset) + len(val_loader.dataset) == 3
    batch = next(iter(train_loader))
    assert torch.is_tensor(batch["input_ids"])
    assert torch.is_tensor(batch["attention_mask"])
    assert torch.is_tensor(batch["labels"])


def test_run_training_splits_dataset_before_creating_loaders():
    # Depuis SCRUM-48, le runner vit dans core.trainer_runner (et non plus dans
    # api). run_training y référence ses dépendances par imports directs au
    # niveau du module (create_dataloaders, load_config, ...). Il faut donc
    # patcher core.trainer_runner.* — patcher api.* est un no-op à l'exécution.
    from core import trainer_runner as _runner

    raw = Dataset.from_dict({
        "text": ["Bonjour", "Hello", "Très bien", "Good"],
        "label": [0, 1, 2, 1],
        "lang_code": ["fr", "en", "fr", "en"],
    })
    job_id = "job-split"
    _jobs[job_id] = TrainJob(job_id=job_id, status=JobStatus.PENDING)

    cfg = {
        "model_name": "distilbert-base-multilingual-cased",
        "max_length": 16,
        "batch_size": 2,
        "num_workers": 0,
        "epochs": 1,
        "learning_rate": 1e-4,
        "weight_decay": 0.01,
        "warmup_ratio": 0.1,
        "device": "cpu",
    }

    fake_tokenizer = MagicMock(save_pretrained=MagicMock())

    with patch.object(_runner, "load_config", return_value=cfg), \
         patch.object(_runner, "load_raw_dataset", return_value=raw), \
         patch.object(_runner, "augment_dataset", side_effect=lambda ds, **kwargs: ds), \
         patch.object(_runner, "create_dataloaders", return_value=(MagicMock(), MagicMock())) as mock_create_dataloaders, \
         patch.object(_runner.AutoTokenizer, "from_pretrained", return_value=fake_tokenizer), \
         patch.object(_runner, "build_model", return_value=MagicMock()), \
         patch.object(_runner, "save_model_version", return_value="fake_model_dir") as mock_save_model_version, \
         patch.object(_runner, "Trainer") as mock_trainer_cls, \
         patch.object(_runner, "TEST_MODE", False):
        mock_trainer = MagicMock()
        mock_trainer_cls.return_value = mock_trainer

        _run_training(job_id, TrainRequest())

    assert _jobs[job_id].status == JobStatus.COMPLETED
    assert mock_create_dataloaders.call_count == 1
    assert len(mock_create_dataloaders.call_args.args) == 3
    assert mock_create_dataloaders.call_args.args[0].__class__.__name__ == "Dataset"
    assert mock_create_dataloaders.call_args.args[1].__class__.__name__ == "Dataset"
    assert mock_create_dataloaders.call_args.args[2]["device"] == "cpu"
    mock_trainer.train.assert_called_once()
    # Depuis le refactor, le runner persiste via save_model_version() (dossiers
    # versionnés dans experiments/models/<timestamp>/) et non plus via
    # trainer.save("./sentiment_model_final") réservé à train.py.
    mock_save_model_version.assert_called_once()


def test_cancel_training_marks_job_cancelled_and_sets_event():
    # Depuis SCRUM-48, cancel_training vit dans core.trainer_runner et suit son
    # propre dict _job_cancel_events (pas celui exposé par api).
    from core import trainer_runner as _runner
    job_id = "job-cancel"
    _jobs[job_id] = TrainJob(job_id=job_id, status=JobStatus.RUNNING)
    _runner._job_cancel_events[job_id] = threading.Event()

    job = cancel_training(job_id)

    assert job.status == JobStatus.CANCELLED
    assert _runner._job_cancel_events[job_id].is_set()


@patch.dict(os.environ, {"API_KEY": "dev"})
def test_job_store_persists_and_loads_jobs():
    job_id = "job-persisted"
    job = TrainJob(job_id=job_id, status=JobStatus.PENDING, step="queued")

    # Depuis SCRUM-48, PersistentJobStore persiste lui-même (écriture SQLite dans
    # __setitem__) : inutile d'appeler d'anciens helpers api._persist_job/_load_jobs.
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "jobs.db")
        api.PersistentJobStore(path=db_path)[job_id] = job

        # Un second store sur le même fichier doit recharger le job persisté.
        reloaded = api.PersistentJobStore(path=db_path)
        assert job_id in reloaded
        assert reloaded[job_id].status == JobStatus.PENDING
        assert reloaded[job_id].step == "queued"

# ------------------------------------------------------------------ #
# Tests pagination / filtrage GET /train/jobs  (SCRUM-49)              #
# ------------------------------------------------------------------ #
def test_list_jobs_pagination():
    """list_jobs respecte limit et offset avec un tri DESC par started_at."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "jobs.db")
        store = api.PersistentJobStore(path=db_path)

        for i in range(10):
            store[f"job-{i}"] = TrainJob(
                job_id=f"job-{i}",
                status=JobStatus.COMPLETED,
                started_at=1000.0 + i,
            )

        # limit=5, offset=0 → 5 premiers (tri DESC → job-9, job-8, job-7, job-6, job-5)
        items, total = store.list_jobs(limit=5, offset=0)
        assert total == 10
        assert len(items) == 5
        assert [j.job_id for j in items] == ["job-9", "job-8", "job-7", "job-6", "job-5"]

        # offset=5 → page suivante
        items, total = store.list_jobs(limit=5, offset=5)
        assert total == 10
        assert len(items) == 5
        assert [j.job_id for j in items] == ["job-4", "job-3", "job-2", "job-1", "job-0"]

        # offset au-delà du total → liste vide mais total correct
        items, total = store.list_jobs(limit=5, offset=100)
        assert total == 10
        assert len(items) == 0


def test_list_jobs_filter_by_status():
    """list_jobs filtre correctement par status (tous les status supportés)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "jobs.db")
        store = api.PersistentJobStore(path=db_path)

        store["j-pending"] = TrainJob(job_id="j-pending", status=JobStatus.PENDING, step="queued")
        store["j-running"] = TrainJob(job_id="j-running", status=JobStatus.RUNNING, step="training", started_at=1000.0)
        store["j-completed"] = TrainJob(job_id="j-completed", status=JobStatus.COMPLETED, started_at=1100.0)
        store["j-failed"] = TrainJob(job_id="j-failed", status=JobStatus.FAILED, started_at=1050.0)
        store["j-cancelled"] = TrainJob(job_id="j-cancelled", status=JobStatus.CANCELLED, started_at=900.0)

        for st in ("pending", "running", "completed", "failed", "cancelled"):
            items, total = store.list_jobs(status=st)
            assert total == 1
            assert len(items) == 1

        # Aucun filtre → tous les jobs
        items, total = store.list_jobs()
        assert total == 5
        assert len(items) == 5

        # Filtre avec pagination
        items, total = store.list_jobs(status="completed", limit=1, offset=0)
        assert total == 1
        assert len(items) == 1


def test_list_jobs_sorted_by_started_at_desc():
    """list_jobs trie par started_at DESC ; les jobs sans started_at (NULL) arrivent en dernier."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "jobs.db")
        store = api.PersistentJobStore(path=db_path)

        store["old"] = TrainJob(job_id="old", status=JobStatus.COMPLETED, started_at=1000.0)
        store["newer"] = TrainJob(job_id="newer", status=JobStatus.COMPLETED, started_at=2000.0)
        store["newest"] = TrainJob(job_id="newest", status=JobStatus.COMPLETED, started_at=3000.0)
        store["pending"] = TrainJob(job_id="pending", status=JobStatus.PENDING)  # started_at=None

        items, total = store.list_jobs()
        assert total == 4
        assert [j.job_id for j in items] == ["newest", "newer", "old", "pending"]


def test_job_store_creates_updated_at_index():
    """Le store crée l'index idx_jobs_updated_at sur updated_at au moment de l'initialisation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "jobs.db")
        api.PersistentJobStore(path=db_path)

        conn = sqlite3.connect(db_path)
        indexes = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_jobs_updated_at'"
        ).fetchall()
        conn.close()

        assert len(indexes) == 1

""" def test_cleanup_old_jobs_removes_expired_completed_jobs():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "jobs.db")

        old_job = TrainJob(job_id="old-job", status=JobStatus.COMPLETED, step="completed")
        fresh_job = TrainJob(job_id="fresh-job", status=JobStatus.RUNNING, step="training")

        api.TEST_MODE = True
        api._jobs = api.PersistentJobStore(path=db_path)

        with patch.object(api, "JOB_STORE_PATH", db_path):
            api._jobs[old_job.job_id] = old_job
            api._jobs[fresh_job.job_id] = fresh_job

            api._jobs.update_job_timestamp(old_job.job_id, time.time() - 90 * 86400)

            result = api.cleanup_old_jobs(max_age_days=30, dry_run=False)

            assert result["deleted"] == 1
            assert old_job.job_id not in api._get_job_store()
            assert fresh_job.job_id in api._get_job_store()
"""



def test_evaluate_pads_variable_length_sequences():
    class DummyModel(torch.nn.Module):
        def forward(self, input_ids, attention_mask=None, **kwargs):
            batch_size = input_ids.size(0)
            return SimpleNamespace(logits=torch.randn(batch_size, 3))

    tokenizer = AutoTokenizer.from_pretrained("distilbert-base-multilingual-cased")
    dataset = Dataset.from_dict({
        "text": [
            "Bonjour le monde",
            "Salut",
            "Très bon produit, je recommande",
            "Mauvais achat",
        ],
        "label": [0, 1, 2, 0],
    })

    tokenized = dataset.map(
        lambda batch: tokenizer(batch["text"], truncation=True, max_length=8),
        batched=True,
    )
    if "label" in tokenized.column_names and "labels" not in tokenized.column_names:
        tokenized = tokenized.rename_column("label", "labels")

    metrics = evaluate(DummyModel(), tokenizer, tokenized, batch_size=2)

    assert "accuracy" in metrics
    assert "f1_macro" in metrics

