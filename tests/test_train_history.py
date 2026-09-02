"""Tests SCRUM-73 : historique des métriques d'entraînement (loss / F1 par epoch).

Couvre :
  - la persistance dans le SQLite existant (table train_metrics),
  - GET /train/history/{job_id} (200 avec métriques, liste vide, 404).
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("API_KEY", "test-key")
# Isolation : ne jamais toucher à la vraie base experiments/jobs.db.
os.environ.setdefault(
    "JOB_STORE_PATH",
    os.path.join(tempfile.gettempdir(), "thinktuning-test-jobs-history.db"),
)

import api  # noqa: E402
from core.job_store import get_job_store  # noqa: E402
from core.models import JobStatus, TrainJob  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture()
def client():
    with TestClient(api.app) as test_client:
        yield test_client


@pytest.fixture()
def store_with_job():
    store = get_job_store()
    job = TrainJob(job_id="11111111-1111-1111-1111-111111111111", status=JobStatus.COMPLETED)
    store[job.job_id] = job
    store.save_epoch_metrics(
        job.job_id,
        [
            {"epoch": 1, "loss": 1.2, "f1_macro": 0.55, "accuracy": 0.6},
            {"epoch": 2, "loss": 0.9, "f1_macro": 0.7, "accuracy": 0.75},
        ],
    )
    return store


def test_save_and_get_epoch_metrics(store_with_job):
    store = store_with_job
    rows = store.get_job_metrics("11111111-1111-1111-1111-111111111111")
    assert [r["epoch"] for r in rows] == [1, 2]
    assert rows[0]["loss"] == pytest.approx(1.2)
    assert rows[1]["f1_macro"] == pytest.approx(0.7)


def test_history_endpoint_returns_metrics(client, store_with_job):
    resp = client.get("/train/history/11111111-1111-1111-1111-111111111111", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["job_id"] == "11111111-1111-1111-1111-111111111111"
    assert len(body["epochs"]) == 2
    assert body["epochs"][0]["loss"] == pytest.approx(1.2)
    assert body["epochs"][1]["f1_macro"] == pytest.approx(0.7)


def test_history_endpoint_empty_for_known_job(client):
    store = get_job_store()
    job = TrainJob(job_id="22222222-2222-2222-2222-222222222222", status=JobStatus.RUNNING)
    store[job.job_id] = job
    resp = client.get("/train/history/22222222-2222-2222-2222-222222222222", headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json()["epochs"] == []


def test_history_endpoint_404_unknown_job(client):
    resp = client.get("/train/history/does-not-exist", headers=HEADERS)
    assert resp.status_code == 404
