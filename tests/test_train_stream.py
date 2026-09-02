"""Tests WebSocket GET /train/stream/{job_id} : métriques live pendant
l'entraînement (loss / F1 epoch par epoch), fermeture propre à la fin.

Couvre :
  - l'auth par jeton (query param `?token=`, jeton dédié DASHBOARD_WS_TOKEN),
  - job inconnu -> rejet,
  - diffusion epoch par epoch pendant un job "running",
  - événement "end" + fermeture propre quand le job devient terminal,
  - job déjà terminé -> historique complet + "end" immédiat,
  - SQLitePollingEventsSource.get_new_events (pagination par last_epoch).
"""
import os
import tempfile
import threading
import time

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("API_KEY", "test-key")
# Isolation : ne jamais toucher à la vraie base experiments/jobs.db.
os.environ.setdefault(
    "JOB_STORE_PATH",
    os.path.join(tempfile.gettempdir(), "thinktuning-test-jobs-stream.db"),
)

import api  # noqa: E402
from core.job_store import get_job_store  # noqa: E402
from core.models import JobStatus, TrainJob  # noqa: E402
from core.training_events import (  # noqa: E402
    SQLitePollingEventsSource,
    reset_events_source_for_tests,
)

TOKEN = "test-key"


@pytest.fixture()
def client():
    reset_events_source_for_tests()
    with TestClient(api.app) as test_client:
        yield test_client


def _make_job(status=JobStatus.RUNNING):
    """Crée un job avec un ID unique (isolation : la base SQLite de test
    persiste entre les runs, il ne faut donc jamais réutiliser un job_id)."""
    import uuid

    store = get_job_store()
    job = TrainJob(job_id=str(uuid.uuid4()), status=status)
    store[job.job_id] = job
    return job


def test_events_source_pagination():
    """get_new_events ne renvoie que les epochs > last_epoch, triées."""
    job = _make_job()
    store = get_job_store()
    store.save_epoch_metrics(
        job.job_id,
        [
            {"epoch": 1, "loss": 1.2, "f1_macro": 0.5, "accuracy": 0.6},
            {"epoch": 2, "loss": 0.9, "f1_macro": 0.7, "accuracy": 0.75},
        ],
    )
    source = SQLitePollingEventsSource()
    events = source.get_new_events_sync(job.job_id, 0)
    assert [e["epoch"] for e in events] == [1, 2]
    assert events[0]["type"] == "epoch"
    assert events[1]["f1_macro"] == pytest.approx(0.7)
    # Pagination : rien de nouveau après l'epoch 2.
    assert source.get_new_events_sync(job.job_id, 2) == []


def test_stream_unknown_job_sends_error_and_closes(client):
    """Job inconnu : la connexion est acceptée puis fermée avec un
    événement d'erreur (pas d'exception côté client)."""
    with client.websocket_connect(
        f"/train/stream/does-not-exist?token={TOKEN}"
    ) as ws:
        msg = ws.receive_json()
        assert msg == {"type": "error", "detail": "job_id introuvable"}


def test_stream_invalid_token_rejected(client):
    job = _make_job()
    with pytest.raises(Exception):
        with client.websocket_connect(f"/train/stream/{job.job_id}?token=wrong"):
            pass


def test_stream_completed_job_sends_history_and_end(client):
    job = _make_job(status=JobStatus.COMPLETED)
    store = get_job_store()
    store.save_epoch_metrics(
        job.job_id,
        [
            {"epoch": 1, "loss": 1.5, "f1_macro": 0.4, "accuracy": 0.5},
            {"epoch": 2, "loss": 1.0, "f1_macro": 0.6, "accuracy": 0.7},
        ],
    )
    with client.websocket_connect(
        f"/train/stream/{job.job_id}?token={TOKEN}"
    ) as ws:
        step = ws.receive_json()
        e1 = ws.receive_json()
        e2 = ws.receive_json()
        end = ws.receive_json()
    assert step == {"type": "step", "step": "queued"}
    assert e1["type"] == "epoch" and e1["epoch"] == 1
    assert e1["loss"] == pytest.approx(1.5)
    assert e2["type"] == "epoch" and e2["epoch"] == 2
    assert e2["f1_macro"] == pytest.approx(0.6)
    assert end == {"type": "end", "status": "completed"}


def test_stream_live_epochs_then_end(client):
    """Job 'running' : l'étape courante puis les epochs arrivent au fil de
    l'eau ; quand le job passe à 'completed', le serveur envoie 'end' et
    ferme la connexion."""
    job = _make_job(status=JobStatus.RUNNING)
    store = get_job_store()

    def simulate_training():
        time.sleep(0.2)
        j = store[job.job_id]
        j.step = "training"
        store[job.job_id] = j
        store.save_epoch_metrics(
            job.job_id,
            [{"epoch": 1, "loss": 2.0, "f1_macro": 0.3, "accuracy": 0.4}],
        )
        time.sleep(0.3)
        store.save_epoch_metrics(
            job.job_id,
            [{"epoch": 2, "loss": 1.1, "f1_macro": 0.65, "accuracy": 0.72}],
        )
        j = store[job.job_id]
        j.status = JobStatus.COMPLETED
        store[job.job_id] = j

    thread = threading.Thread(target=simulate_training, daemon=True)
    thread.start()

    received = []
    with client.websocket_connect(
        f"/train/stream/{job.job_id}?token={TOKEN}"
    ) as ws:
        received.append(ws.receive_json())  # step initial (queued)
        received.append(ws.receive_json())  # step training
        received.append(ws.receive_json())  # epoch 1
        received.append(ws.receive_json())  # epoch 2
        received.append(ws.receive_json())  # end
    thread.join(timeout=5)

    types = [m["type"] for m in received]
    assert types == ["step", "step", "epoch", "epoch", "end"]
    assert received[0] == {"type": "step", "step": "queued"}
    assert received[1] == {"type": "step", "step": "training"}
    assert received[2]["epoch"] == 1
    assert received[2]["loss"] == pytest.approx(2.0)
    assert received[3]["epoch"] == 2
    assert received[3]["f1_macro"] == pytest.approx(0.65)
    assert received[4]["status"] == "completed"


def test_stream_no_stall_outside_training_step(client, monkeypatch):
    """Anti-stall : un job 'running' à l'étape loading_model (chargement du
    modèle, tokenizers, ...) peut durer longtemps sans nouvel epoch — le
    stall ne doit PAS être déclenché hors de l'étape 'training'."""
    monkeypatch.setattr("api.routes.train.STALL_MINUTES", 0.01)
    job = _make_job(status=JobStatus.RUNNING)
    store = get_job_store()
    j = store[job.job_id]
    j.step = "loading_model"
    store[job.job_id] = j

    with client.websocket_connect(
        f"/train/stream/{job.job_id}?token={TOKEN}"
    ) as ws:
        first = ws.receive_json()  # step initial
        assert first == {"type": "step", "step": "loading_model"}
        # Le polling est de 0,5 s : après quelques cycles, STALL_MINUTES
        # (0,01 min = 0,6 s) est dépassé sans nouvel epoch -> aucun
        # événement "stalled" ne doit arriver ; on reçoit encore du "step"
        # (aucun changement, rien n'est renvoyé) -> utiliser receive avec
        # timeout n'est pas trivial via TestClient : on vérifie simplement
        # que la connexion reste ouverte en rejouant un epoch tardif.
        time.sleep(1.2)
        store.save_epoch_metrics(
            job.job_id,
            [{"epoch": 1, "loss": 1.0, "f1_macro": 0.5, "accuracy": 0.6}],
        )
        msg = ws.receive_json()
        assert msg["type"] == "epoch"
        assert msg["epoch"] == 1


def test_stream_stall_during_training_step(client, monkeypatch):
    """Anti-stall : job 'running' à l'étape 'training' sans nouvel epoch
    depuis plus de STALL_MINUTES -> événement 'stalled' + fermeture."""
    monkeypatch.setattr("api.routes.train.STALL_MINUTES", 0.01)
    job = _make_job(status=JobStatus.RUNNING)
    store = get_job_store()
    j = store[job.job_id]
    j.step = "training"
    store[job.job_id] = j

    with client.websocket_connect(
        f"/train/stream/{job.job_id}?token={TOKEN}"
    ) as ws:
        assert ws.receive_json() == {"type": "step", "step": "training"}
        msg = ws.receive_json()
        assert msg["type"] == "stalled"
        assert msg["last_epoch"] == 0
        assert msg["minutes"] >= 0.0


def test_stream_sends_logs_and_progress(client):
    """Nouveauté : événements "log" (logs serveur par étape) et "progress"
    (avancement batch-par-batch + pourcentage global + état des étapes),
    rejoués à la connexion puis à chaque changement."""
    import logging as _logging

    from core import job_logs

    job = _make_job(status=JobStatus.RUNNING)
    store = get_job_store()
    j = store[job.job_id]
    j.step = "training"
    j.progress = {
        "step": "training",
        "phase": "train",
        "epoch": 1,
        "epochs_total": 2,
        "batch": 1,
        "batches_total": 2,
        "batch_pct": 50.0,
        "global_pct": 38.5,
        "rate_it_s": 7.73,
        "eta": 7.7,
        "steps": {
            "queued": {"status": "done"},
            "training": {"status": "active"},
            "done": {"status": "pending"},
        },
    }
    store[job.job_id] = j

    # Logs capturés comme le ferait run_training (thread attaché au job).
    job_logs.ensure_job_log_handler()

    def worker():
        job_logs.attach_job_logging(job.job_id)
        try:
            job_logs.set_job_log_step("training")
            _logging.getLogger("test").info("Début de l'entraînement")
        finally:
            job_logs.detach_job_logging()

    worker()

    with client.websocket_connect(
        f"/train/stream/{job.job_id}?token={TOKEN}"
    ) as ws:
        first = ws.receive_json()
        assert first == {"type": "step", "step": "training"}
        second = ws.receive_json()
        assert second["type"] == "log"
        assert second["message"] == "Début de l'entraînement"
        assert second["step"] == "training"
        third = ws.receive_json()
        assert third["type"] == "progress"
        assert third["global_pct"] == pytest.approx(38.5)
        assert third["batch"] == 1 and third["batches_total"] == 2
        assert third["steps"]["training"]["status"] == "active"
        # Ensuite : fin propre (statut terminal simulé).
        j = store[job.job_id]
        j.status = JobStatus.COMPLETED
        store[job.job_id] = j
        end = ws.receive_json()
        assert end["type"] == "end"
        assert end["status"] == "completed"
