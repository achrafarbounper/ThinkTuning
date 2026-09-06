# project/tests/test_api_v1_training.py
"""Tests des endpoints training v1 (Phase 3d — découplage du noyau /train).

Les fakes substituent les trois ports (jobs / runner / schedules) via
``app.dependency_overrides`` (pattern officiel FastAPI) : AUCUN entraînement
réel n'est lancé, aucune infrastructure touchée. Les tests WebSocket visent
le handler PARTAGÉ avec le legacy (délégation v1) : ils utilisent le store
SQLite isolé (même convention que test_train_stream.py).
"""

import os
import tempfile

os.environ.setdefault("API_KEY", "test-key")
# Isolation : ne jamais toucher à la vraie base experiments/jobs.db
# (même base de test que test_train_stream.py, job_ids uuid => pas de collision).
os.environ.setdefault(
    "JOB_STORE_PATH",
    os.path.join(tempfile.gettempdir(), "thinktuning-test-jobs-stream.db"),
)

import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import api  # noqa: F401
from api import app
from api.dependencies.composition import (
    get_training_jobs_port,
    get_training_runner_port,
    get_training_schedules_port,
)
from app.domain.errors import NotFoundError
from core.job_store import get_job_store
from core.models import JobStatus, TrainJob, TrainRequest

client = TestClient(app)

AUTH = {"X-API-Key": "test-key"}


# ---------------------------------------------------------------------------
# Fakes des trois ports (déterministes, aucune infrastructure)
# ---------------------------------------------------------------------------
class FakeJobs:
    def __init__(self) -> None:
        self.jobs: dict[str, TrainJob] = {}
        self.metrics_rows: dict[str, list[dict]] = {}

    def get(self, job_id: str):
        return self.jobs.get(job_id)

    def list(self, *, status, limit, offset):
        items = list(self.jobs.values())
        items.reverse()  # tri started_at DESC simulé
        if status is not None:
            items = [j for j in items if j.status.value == status]
        total = len(items)
        return items[offset : offset + limit], total

    def metrics(self, job_id: str):
        return list(self.metrics_rows.get(job_id, []))


class FakeRunner:
    def __init__(self) -> None:
        self.started: list[TrainRequest] = []
        self.known: set[str] = set()

    def start(self, request: TrainRequest):
        job = TrainJob(job_id=f"job-{len(self.started) + 1}", status=JobStatus.PENDING)
        self.started.append(request)
        self.known.add(job.job_id)
        return job

    def cancel(self, job_id: str):
        if job_id not in self.known:
            raise NotFoundError("job_id introuvable")
        return TrainJob(
            job_id=job_id,
            status=JobStatus.CANCELLED,
            step="cancelled",
            error="Training cancelled by user",
            finished_at=123.0,
        )


class FakeSchedules:
    def __init__(self) -> None:
        self.items: list[dict] = []

    def create(self, *, cron, interval_minutes, train_request):
        if cron is None and interval_minutes is None:
            raise ValueError("cron ou interval_minutes requis")
        item = {
            "schedule_id": f"sched-{len(self.items) + 1}",
            "status": "scheduled",
            "trigger": "cron" if cron else "interval",
            "cron": cron,
            "interval_minutes": interval_minutes,
            "next_run_at": 1_700_000_000.0,
            "created_at": 1_699_000_000.0,
            "train_request": train_request,
        }
        self.items.append(item)
        return item

    def list(self):
        return list(self.items)

    def delete(self, schedule_id: str):
        before = len(self.items)
        self.items = [s for s in self.items if s["schedule_id"] != schedule_id]
        return len(self.items) < before


class FakePorts:
    """Namespace des trois fakes (un seul fixture d'override)."""

    def __init__(self) -> None:
        self.jobs = FakeJobs()
        self.runner = FakeRunner()
        self.schedules = FakeSchedules()


@pytest.fixture
def fake_ports():
    """Substitue les trois ports le temps d'un test (auto-nettoyage)."""
    fakes = FakePorts()
    app.dependency_overrides[get_training_jobs_port] = lambda: fakes.jobs
    app.dependency_overrides[get_training_runner_port] = lambda: fakes.runner
    app.dependency_overrides[get_training_schedules_port] = lambda: fakes.schedules
    yield fakes
    app.dependency_overrides.pop(get_training_jobs_port, None)
    app.dependency_overrides.pop(get_training_runner_port, None)
    app.dependency_overrides.pop(get_training_schedules_port, None)


# ---------------------------------------------------------------------------
# HTTP : cycle de vie d'un job
# ---------------------------------------------------------------------------
def test_v1_train_start_returns_202_pending(fake_ports):
    """POST /api/v1/train => 202 + job PENDING, requête transmise au runner."""
    response = client.post(
        "/api/v1/train", json={"max_per_lang": 10, "epochs": 2}, headers=AUTH
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["kind"] == "train"
    assert len(fake_ports.runner.started) == 1
    assert fake_ports.runner.started[0].max_per_lang == 10


def test_v1_train_start_requires_api_key(fake_ports):
    """Surface v1 protégée : sans X-API-Key => 401 (parité legacy)."""
    response = client.post("/api/v1/train", json={})

    assert response.status_code == 401, response.text


def test_v1_train_status_200(fake_ports):
    job = TrainJob(job_id="job-x", status=JobStatus.RUNNING, step="training")
    fake_ports.jobs.jobs["job-x"] = job

    response = client.get("/api/v1/train/status/job-x", headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "running"
    assert response.json()["step"] == "training"


def test_v1_train_status_unknown_job_404_envelope(fake_ports):
    """Job inconnu => 404 + payload domaine (message legacy conservé)."""
    response = client.get("/api/v1/train/status/nope", headers=AUTH)

    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"] == "job_id introuvable"


def test_v1_train_cancel_200(fake_ports):
    fake_ports.runner.known.add("job-x")

    response = client.post("/api/v1/train/cancel/job-x", headers=AUTH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["error"] == "Training cancelled by user"


def test_v1_train_cancel_unknown_job_404(fake_ports):
    response = client.post("/api/v1/train/cancel/nope", headers=AUTH)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# HTTP : historique + liste paginée
# ---------------------------------------------------------------------------
def test_v1_train_history_200_maps_epochs(fake_ports):
    """SCRUM-73 : les lignes du store sont mappées en EpochMetric (shape legacy)."""
    fake_ports.jobs.jobs["job-h"] = TrainJob(job_id="job-h", status=JobStatus.COMPLETED)
    fake_ports.jobs.metrics_rows["job-h"] = [
        {"epoch": 1, "loss": 1.2, "f1_macro": 0.5, "accuracy": 0.6},
        {"epoch": 2, "loss": 0.9, "f1_macro": 0.7, "accuracy": 0.75},
    ]

    response = client.get("/api/v1/train/history/job-h", headers=AUTH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["job_id"] == "job-h"
    assert body["epochs"] == [
        {"epoch": 1, "loss": 1.2, "f1_macro": 0.5, "accuracy": 0.6},
        {"epoch": 2, "loss": 0.9, "f1_macro": 0.7, "accuracy": 0.75},
    ]


def test_v1_train_history_empty_for_known_job(fake_ports):
    """Job connu mais sans métriques : liste vide (jamais 404)."""
    fake_ports.jobs.jobs["job-h"] = TrainJob(job_id="job-h", status=JobStatus.RUNNING)

    response = client.get("/api/v1/train/history/job-h", headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.json()["epochs"] == []


def test_v1_train_history_unknown_job_404(fake_ports):
    response = client.get("/api/v1/train/history/nope", headers=AUTH)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


def test_v1_train_jobs_list_shape_and_filter(fake_ports):
    """{total, items, limit, offset} + filtre status (tri DESC simulé)."""
    fake_ports.jobs.jobs["job-1"] = TrainJob(job_id="job-1", status=JobStatus.RUNNING)
    fake_ports.jobs.jobs["job-2"] = TrainJob(job_id="job-2", status=JobStatus.COMPLETED)

    response = client.get(
        "/api/v1/train/jobs",
        params={"status": "completed", "limit": 20, "offset": 0},
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert [j["job_id"] for j in body["items"]] == ["job-2"]
    assert body["limit"] == 20 and body["offset"] == 0


def test_v1_train_jobs_invalid_status_422(fake_ports):
    """Status hors enum : rejet 422 par validation Pydantic (parité legacy)."""
    response = client.get(
        "/api/v1/train/jobs", params={"status": "bogus"}, headers=AUTH
    )

    assert response.status_code == 422, response.text


def test_v1_train_jobs_requires_api_key(fake_ports):
    response = client.get("/api/v1/train/jobs")

    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# HTTP : planifications récurrentes (SCRUM-34)
# ---------------------------------------------------------------------------
def test_v1_train_schedule_202(fake_ports):
    response = client.post(
        "/api/v1/train/schedule",
        json={"train": {"max_per_lang": 500}, "cron": "0 2 * * *"},
        headers=AUTH,
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "scheduled"
    assert body["trigger"] == "cron"
    assert body["cron"] == "0 2 * * *"
    assert fake_ports.schedules.items[0]["train_request"]["max_per_lang"] == 500


def test_v1_train_schedule_invalid_cron_422(fake_ports):
    """Cron à 3 champs : rejet 422 par le validateur Pydantic (parité legacy)."""
    response = client.post(
        "/api/v1/train/schedule",
        json={"train": {}, "cron": "0 2 *"},
        headers=AUTH,
    )

    assert response.status_code == 422, response.text


def test_v1_train_schedule_missing_params_422_envelope(fake_ports):
    """Ni cron ni interval : le use-case mappe ValueError => enveloppe domaine."""
    response = client.post(
        "/api/v1/train/schedule", json={"train": {}}, headers=AUTH
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"


def test_v1_train_schedules_list(fake_ports):
    response = client.get("/api/v1/train/schedules", headers=AUTH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_v1_train_delete_schedule_204_and_404(fake_ports):
    fake_ports.schedules.create(
        cron="0 2 * * *", interval_minutes=None, train_request={}
    )

    ok = client.delete("/api/v1/train/schedules/sched-1", headers=AUTH)
    assert ok.status_code == 204, ok.text
    assert ok.text == ""

    missing = client.delete("/api/v1/train/schedules/nope", headers=AUTH)
    assert missing.status_code == 404, missing.text
    error = missing.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"] == "schedule_id introuvable"


# ---------------------------------------------------------------------------
# WebSocket : délégation au handler partagé avec le legacy
# ---------------------------------------------------------------------------
TOKEN = "test-key"  # API_KEY (DASHBOARD_WS_TOKEN non posé => repli clé API)


@pytest.fixture()
def ws_client():
    """Client avec lifespan complet (comme test_train_stream.py)."""
    with TestClient(app) as test_client:
        yield test_client


def _make_job(status=JobStatus.RUNNING) -> TrainJob:
    """Crée un job au store réel avec un ID unique (isolation SQLite)."""
    store = get_job_store()
    job = TrainJob(job_id=str(uuid.uuid4()), status=status)
    store[job.job_id] = job
    return job


def test_v1_train_stream_unknown_job_sends_error_and_closes(ws_client):
    """Job inconnu : connexion acceptée puis erreur + fermeture (handler partagé)."""
    with ws_client.websocket_connect(
        f"/api/v1/train/stream/does-not-exist?token={TOKEN}"
    ) as ws:
        msg = ws.receive_json()
        assert msg == {"type": "error", "detail": "job_id introuvable"}


def test_v1_train_stream_invalid_token_rejected(ws_client):
    """Jeton invalide : la connexion est rejetée (WebSocketDisconnect).

    Plus précis que le legacy (pytest.raises(Exception) — dette B017) : on
    cible l'exception réellement levée par TestClient sur un refus WS.
    """
    job = _make_job()
    with pytest.raises(WebSocketDisconnect):
        with ws_client.websocket_connect(
            f"/api/v1/train/stream/{job.job_id}?token=wrong"
        ):
            pass


def test_v1_train_stream_completed_job_sends_history_and_end(ws_client):
    """Job déjà terminal : historique complet puis 'end' immédiat (comme legacy)."""
    job = _make_job(status=JobStatus.COMPLETED)
    get_job_store().save_epoch_metrics(
        job.job_id,
        [
            {"epoch": 1, "loss": 1.5, "f1_macro": 0.4, "accuracy": 0.5},
            {"epoch": 2, "loss": 1.0, "f1_macro": 0.6, "accuracy": 0.7},
        ],
    )

    with ws_client.websocket_connect(
        f"/api/v1/train/stream/{job.job_id}?token={TOKEN}"
    ) as ws:
        first = ws.receive_json()
        assert first == {"type": "step", "step": "queued"}
        epochs = [ws.receive_json(), ws.receive_json()]
        assert [e["type"] for e in epochs] == ["epoch", "epoch"]
        assert [e["epoch"] for e in epochs] == [1, 2]
        end = ws.receive_json()
        assert end == {"type": "end", "status": "completed"}
