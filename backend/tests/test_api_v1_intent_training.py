# project/tests/test_api_v1_intent_training.py
"""Tests des endpoints d'entraînement d'intention v1 (Phase 3d-2 — SCRUM-95).

Les fakes substituent les ports (jobs / runner / versioning) via
``app.dependency_overrides`` : AUCUN entraînement réel, AUCUN torch. Les
validations défensives (dataset, version source) et les erreurs de versions
sont simulées côté fake avec les MESSAGES legacy conservés (parité 422).
"""

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from fastapi.testclient import TestClient

import api  # noqa: F401
from api import app
from api.dependencies.composition import (
    get_intent_training_runner_port,
    get_intent_versioning_port,
    get_training_jobs_port,
)
from app.domain.errors import NotFoundError, ValidationError
from core.models import IntentTrainRequest, JobStatus, TrainJob

client = TestClient(app)

AUTH = {"X-API-Key": "test-key"}


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """P1 : /train (et l'entraînement d'intention, même préfixe) a un quota
    5/HEURE — chaque test repart d'un bucket vide."""
    api._reset_rate_limit_buckets()
    yield
    api._reset_rate_limit_buckets()


# ---------------------------------------------------------------------------
# Fakes des trois ports (déterministes, aucune infrastructure)
# ---------------------------------------------------------------------------
class FakeJobs:
    def __init__(self) -> None:
        self.jobs: dict[str, TrainJob] = {}
        self.seen_kinds: list[str | None] = []

    def get(self, job_id: str):
        return self.jobs.get(job_id)

    def list(self, *, status, kind=None, limit, offset):
        self.seen_kinds.append(kind)
        items = list(self.jobs.values())
        items.reverse()  # tri started_at DESC simulé
        if kind is not None:
            items = [j for j in items if j.kind == kind]
        if status is not None:
            items = [j for j in items if j.status.value == status]
        total = len(items)
        return items[offset : offset + limit], total

    def metrics(self, job_id: str):
        return []


class FakeIntentRunner:
    def __init__(self) -> None:
        self.prechecked: list[IntentTrainRequest] = []
        self.precheck_error: ValidationError | None = None
        self.started: list[IntentTrainRequest] = []
        self.known: set[str] = set()

    def precheck(self, request: IntentTrainRequest) -> None:
        self.prechecked.append(request)
        if self.precheck_error is not None:
            raise self.precheck_error

    def start(self, request: IntentTrainRequest) -> TrainJob:
        job = TrainJob(
            job_id=f"intent-{len(self.started) + 1}",
            status=JobStatus.PENDING,
            kind="intent",
        )
        self.started.append(request)
        self.known.add(job.job_id)
        return job

    def cancel(self, job_id: str) -> TrainJob:
        if job_id not in self.known:
            raise NotFoundError("job_id introuvable")
        return TrainJob(
            job_id=job_id,
            status=JobStatus.CANCELLED,
            step="cancelled",
            kind="intent",
            error="Training cancelled by user",
            finished_at=123.0,
        )


class FakeIntentVersioning:
    def __init__(self) -> None:
        self.versions: list[str] = []
        self.active: str | None = None
        self.activate_error: ValidationError | None = None
        self.activated: list[str] = []

    def list_versions(self) -> list[str]:
        return list(self.versions)

    def resolve_active_version(self) -> str | None:
        return self.active

    def activate(self, version: str) -> None:
        # Note : le fake lève directement la ValidationError domaine — la
        # conversion RuntimeError -> ValidationError est le rôle de
        # l'ADAPTATEUR (ModuleIntentVersioningAdapter), pas du use case.
        if self.activate_error is not None:
            raise self.activate_error
        self.activated.append(version)


class FakeIntentPorts:
    def __init__(self) -> None:
        self.jobs = FakeJobs()
        self.runner = FakeIntentRunner()
        self.versioning = FakeIntentVersioning()


@pytest.fixture
def fake_intent():
    """Substitue les trois ports le temps d'un test (auto-nettoyage)."""
    fakes = FakeIntentPorts()
    app.dependency_overrides[get_training_jobs_port] = lambda: fakes.jobs
    app.dependency_overrides[get_intent_training_runner_port] = lambda: fakes.runner
    app.dependency_overrides[get_intent_versioning_port] = lambda: fakes.versioning
    yield fakes
    app.dependency_overrides.pop(get_training_jobs_port, None)
    app.dependency_overrides.pop(get_intent_training_runner_port, None)
    app.dependency_overrides.pop(get_intent_versioning_port, None)


# ---------------------------------------------------------------------------
# POST /api/v1/train/intent : precheck puis job
# ---------------------------------------------------------------------------
def test_v1_intent_start_returns_202_kind_intent(fake_intent):
    """POST => 202 + job kind="intent" ; la requête passe par le precheck."""
    response = client.post(
        "/api/v1/train/intent",
        json={"epochs": 2, "batch_size": 16},
        headers=AUTH,
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "pending"
    assert body["kind"] == "intent"
    # Precheck appelé AVANT la création du job (validations défensives).
    assert len(fake_intent.runner.prechecked) == 1
    assert fake_intent.runner.prechecked[0].epochs == 2
    assert fake_intent.runner.prechecked[0].max_length == 64  # max_length=64 (doc 13, item 2a)


def test_v1_intent_start_requires_api_key(fake_intent):
    response = client.post("/api/v1/train/intent", json={})

    assert response.status_code == 401, response.text


def test_v1_intent_start_dataset_missing_422(fake_intent):
    """Dataset absent : 422 AVANT création du job (message legacy conservé)."""
    fake_intent.runner.precheck_error = ValidationError(
        "Dataset introuvable : data/intent_dataset.jsonl"
    )

    response = client.post(
        "/api/v1/train/intent", json={"epochs": 2}, headers=AUTH
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["message"] == "Dataset introuvable : data/intent_dataset.jsonl"
    # Aucun job créé : le precheck court-circuite le start.
    assert fake_intent.runner.started == []


def test_v1_intent_start_invalid_base_model_422(fake_intent):
    """Version source invalide : 422 (message du store de versions conservé)."""
    fake_intent.runner.precheck_error = ValidationError(
        "Version de modèle d'intention inconnue : 20990101T000000Z"
    )

    response = client.post(
        "/api/v1/train/intent",
        json={"base_model_version": "20990101T000000Z"},
        headers=AUTH,
    )

    assert response.status_code == 422, response.text
    assert fake_intent.runner.started == []


# ---------------------------------------------------------------------------
# Statut / annulation (use case partagé avec le sentiment)
# ---------------------------------------------------------------------------
def test_v1_intent_status_200(fake_intent):
    fake_intent.jobs.jobs["job-i"] = TrainJob(
        job_id="job-i", status=JobStatus.RUNNING, step="training", kind="intent"
    )

    response = client.get("/api/v1/train/intent/status/job-i", headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "running"
    assert response.json()["kind"] == "intent"


def test_v1_intent_status_unknown_404(fake_intent):
    response = client.get("/api/v1/train/intent/status/nope", headers=AUTH)

    assert response.status_code == 404, response.text
    error = response.json()["error"]
    assert error["code"] == "not_found"
    assert error["message"] == "job_id introuvable"


def test_v1_intent_cancel_200(fake_intent):
    fake_intent.runner.known.add("job-i")

    response = client.post("/api/v1/train/intent/cancel/job-i", headers=AUTH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["kind"] == "intent"


def test_v1_intent_cancel_unknown_404(fake_intent):
    response = client.post("/api/v1/train/intent/cancel/nope", headers=AUTH)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# GET /api/v1/train/intent/jobs : filtre kind="intent"
# ---------------------------------------------------------------------------
def test_v1_intent_jobs_filters_kind(fake_intent):
    """Le store partagé est filtré : aucun job sentiment/pipeline ne fuite."""
    fake_intent.jobs.jobs["job-train"] = TrainJob(
        job_id="job-train", status=JobStatus.RUNNING, kind="train"
    )
    fake_intent.jobs.jobs["job-intent"] = TrainJob(
        job_id="job-intent", status=JobStatus.RUNNING, kind="intent"
    )

    response = client.get("/api/v1/train/intent/jobs", headers=AUTH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert [j["job_id"] for j in body["items"]] == ["job-intent"]
    # Le use case a bien demandé le filtre kind="intent" au port.
    assert fake_intent.jobs.seen_kinds == ["intent"]


# ---------------------------------------------------------------------------
# Versions / activation
# ---------------------------------------------------------------------------
def test_v1_intent_versions_shape(fake_intent):
    """{total, items: [str], active} — shape legacy (tri DESC du store)."""
    fake_intent.versioning.versions = ["20260905T120000Z", "20260901T080000Z"]
    fake_intent.versioning.active = "20260901T080000Z"

    response = client.get("/api/v1/train/intent/versions", headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "total": 2,
        "items": ["20260905T120000Z", "20260901T080000Z"],
        "active": "20260901T080000Z",
    }


def test_v1_intent_versions_active_none_when_no_model(fake_intent):
    """Aucun modèle : active=None (repli de règles côté classifieur)."""
    response = client.get("/api/v1/train/intent/versions", headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.json() == {"total": 0, "items": [], "active": None}


def test_v1_intent_activate_200(fake_intent):
    response = client.post(
        "/api/v1/train/intent/activate",
        json={"version": "20260905T120000Z"},
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "activated", "version": "20260905T120000Z"}
    assert fake_intent.versioning.activated == ["20260905T120000Z"]


def test_v1_intent_activate_unknown_version_422(fake_intent):
    """Version inconnue : 422 enveloppe domaine (l'adaptateur convertit le
    RuntimeError du store en ValidationError — cf. ModuleIntentVersioningAdapter)."""
    fake_intent.versioning.activate_error = ValidationError(
        "Version de modèle d'intention inconnue : nope"
    )

    response = client.post(
        "/api/v1/train/intent/activate", json={"version": "nope"}, headers=AUTH
    )

    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert "inconnue" in error["message"]


def test_v1_intent_activate_requires_api_key(fake_intent):
    response = client.post("/api/v1/train/intent/activate", json={"version": "x"})

    assert response.status_code == 401, response.text
