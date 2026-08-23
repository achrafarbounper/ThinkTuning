# project/tests/test_api_health.py

import os

os.environ.setdefault("API_KEY", "test-key")

from fastapi.testclient import TestClient

import api  # noqa: F401  (initialise MODULE_ROOT/job store avant le routage)
from api import app
from core.models import JobStatus, TrainJob


client = TestClient(app)


def test_health_without_model(monkeypatch, tmp_path):
    empty_root = tmp_path / "no-models"
    monkeypatch.setattr("core.model_versioning.MODEL_ROOT", str(empty_root))
    monkeypatch.setattr("api.routes.health.MODEL_ROOT", str(empty_root))
    monkeypatch.setattr("api.routes.health.get_job_store", lambda: {})

    response = client.get("/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["model_available"] is False
    assert body["model_dir"] is None
    assert body["active_jobs"] == 0
    assert body["maintenance_mode"] is False


def test_health_with_model(monkeypatch, tmp_path):
    root = tmp_path / "models"
    version_dir = root / "20260101T000000Z"
    version_dir.mkdir(parents=True)
    (version_dir / "model.pt").write_bytes(b"fake-weights")
    monkeypatch.setattr("core.model_versioning.MODEL_ROOT", str(root))
    monkeypatch.setattr("api.routes.health.MODEL_ROOT", str(root))
    monkeypatch.setattr("api.routes.health.get_job_store", lambda: {})

    response = client.get("/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model_available"] is True
    assert body["model_dir"] == str(version_dir)


def test_health_counts_only_running_jobs(monkeypatch):
    store = {
        "job-running": TrainJob(job_id="job-running", status=JobStatus.RUNNING),
        "job-pending": TrainJob(job_id="job-pending", status=JobStatus.PENDING),
        "job-completed": TrainJob(job_id="job-completed", status=JobStatus.COMPLETED),
    }
    monkeypatch.setattr("api.routes.health.get_job_store", lambda: store)

    response = client.get("/health")

    assert response.status_code == 200, response.text
    assert response.json()["active_jobs"] == 1


def test_health_reflects_maintenance_mode(monkeypatch):
    monkeypatch.setattr("api.routes.health.is_maintenance_mode", lambda: True)

    response = client.get("/health")

    assert response.status_code == 200, response.text
    assert response.json()["maintenance_mode"] is True