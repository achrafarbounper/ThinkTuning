# project/tests/test_api_v1_pipeline.py
"""Tests des endpoints pipeline v1 (Phase 3d-5).

Parité par construction via délégation aux handlers legacy ; fakes du job
store + no-op sur le runner (aucun pipeline réel lancé).
"""

import os

os.environ.setdefault("API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402, F401
from api import app  # noqa: E402
from core.models import JobStatus, TrainJob

client = TestClient(app)
AUTH = {"X-API-Key": "test-key"}


class _FakeJobStore:
    """Miroir du contrat job_store consommé par les handlers pipeline."""

    def __init__(self) -> None:
        self.jobs: dict[str, TrainJob] = {}

    def __setitem__(self, job_id: str, job: TrainJob) -> None:
        self.jobs[job_id] = job

    def get(self, job_id: str) -> TrainJob | None:
        return self.jobs.get(job_id)

    def list_jobs(self, *, status=None, limit=100, offset=0):
        items = list(self.jobs.values())
        if status is not None:
            # Le handler legacy convertit l'enum en string ('running', ...).
            items = [j for j in items if j.status == status or j.status.value == status]
        total = len(items)
        return items[offset : offset + limit], total


def _install_pipeline_fakes(monkeypatch) -> _FakeJobStore:
    fake = _FakeJobStore()
    monkeypatch.setattr("api.routes.pipeline.get_job_store", lambda: fake)
    monkeypatch.setattr("api.routes.pipeline.run_pipeline", lambda *a, **k: None)
    monkeypatch.setattr("api.routes.pipeline.get_cancel_event", lambda *a, **k: None)
    return fake


def test_pipeline_requires_key():
    assert client.post("/api/v1/pipeline", json={}).status_code == 401


def test_start_pipeline_202(monkeypatch):
    _install_pipeline_fakes(monkeypatch)
    response = client.post(
        "/api/v1/pipeline",
        json={"input_path": "experiments/pipeline/x/labeled.jsonl"},
        headers=AUTH,
    )
    assert response.status_code == 202
    body = response.json()
    assert body["job_id"]
    assert body["status"] == "pending"


def test_start_pipeline_missing_input_is_422(monkeypatch):
    _install_pipeline_fakes(monkeypatch)
    response = client.post("/api/v1/pipeline", json={"input_path": ""}, headers=AUTH)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_pipeline_status_and_404(monkeypatch):
    fake = _install_pipeline_fakes(monkeypatch)
    fake["job-1"] = TrainJob(job_id="job-1", status=JobStatus.RUNNING)

    ok = client.get("/api/v1/pipeline/status/job-1", headers=AUTH)
    assert ok.status_code == 200
    assert ok.json()["job_id"] == "job-1"

    missing = client.get("/api/v1/pipeline/status/nope", headers=AUTH)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_pipeline_cancel(monkeypatch):
    fake = _install_pipeline_fakes(monkeypatch)
    fake["job-1"] = TrainJob(job_id="job-1", status=JobStatus.CANCELLED)

    def _fake_cancel(job_id: str):
        if job_id not in fake.jobs:
            raise RuntimeError("job_id introuvable")
        return fake.jobs[job_id]

    monkeypatch.setattr("api.routes.pipeline.cancel_pipeline", _fake_cancel)

    ok = client.post("/api/v1/pipeline/cancel/job-1", headers=AUTH)
    assert ok.status_code == 200

    missing = client.post("/api/v1/pipeline/cancel/nope", headers=AUTH)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def test_pipeline_jobs_list(monkeypatch):
    fake = _install_pipeline_fakes(monkeypatch)
    fake["job-1"] = TrainJob(job_id="job-1", status=JobStatus.COMPLETED)
    response = client.get("/api/v1/pipeline/jobs", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["job_id"] == "job-1"

    filtered = client.get("/api/v1/pipeline/jobs?status=running", headers=AUTH)
    assert filtered.json()["total"] == 0
