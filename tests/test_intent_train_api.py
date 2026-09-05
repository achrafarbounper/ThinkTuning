# project/tests/test_intent_train_api.py

"""Tests des routes d'entraînement du classifieur d'intention (SCRUM-95).

Le runner lourd (torch / transformers) est remplacé par des exécutions
simulées qui respectent le contrat du vrai runner (statuts, étapes,
model_path), pour ne rien télécharger ni entraîner. Les tests de versions /
activation redirigent ``core.intent_store.INTENT_MODEL_ROOT`` vers un dossier
temporaire.
"""

import os

os.environ.setdefault("API_KEY", "test-key")

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import api  # noqa: F401
from api import app
from core.models import JobStatus

HEADERS = {"X-API-Key": "test-key"}
client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures : runners simulés + dataset minimal
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_runner(monkeypatch):
    """Runner simulé : RUNNING -> étapes -> COMPLETED (contrat du vrai runner)."""
    from core.job_store import get_job_store

    def _run(job_id, req):
        store = get_job_store()
        job = store.get(job_id)
        job.status = JobStatus.RUNNING
        job.step = "loading_dataset"
        store[job_id] = job
        job.step = "saving_model"
        job.model_path = "experiments/intent_models/FAKE"
        job.status = JobStatus.COMPLETED
        job.finished_at = time.time()
        store[job_id] = job

    monkeypatch.setattr("api.routes.intent_train.run_intent_training", _run)


@pytest.fixture()
def slow_runner(monkeypatch):
    """Runner simulé bloquant : reste "running" jusqu'à l'annulation.

    Reproduit le scénario réel : la route pose CANCELLED immédiatement et le
    thread s'arrête en laissant ce statut en place.
    """
    from core.job_store import get_job_store

    def _run(job_id, req):
        store = get_job_store()
        job = store.get(job_id)
        job.status = JobStatus.RUNNING
        store[job_id] = job
        deadline = time.time() + 10
        while store.get(job_id).status != JobStatus.CANCELLED and time.time() < deadline:
            time.sleep(0.05)

    monkeypatch.setattr("api.routes.intent_train.run_intent_training", _run)


@pytest.fixture()
def intent_dataset(tmp_path):
    """Dataset JSONL minimal chat/action (4 exemples)."""
    path = tmp_path / "intent_dataset.jsonl"
    rows = [
        {"text": "lance l'entraînement du modèle", "label": "action"},
        {"text": "bonjour comment ça va", "label": "chat"},
        {"text": "ouvre le rapport du trimestre", "label": "action"},
        {"text": "merci beaucoup pour ton aide", "label": "chat"},
    ]
    path.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
    )
    return str(path)


def _make_version(root: Path, name: str) -> None:
    """Crée une version d'intention factice (config.json + poids) dans *root*."""
    version_dir = root / name
    version_dir.mkdir(parents=True)
    (version_dir / "config.json").write_text(
        json.dumps({"model_type": "bert"}), encoding="utf-8"
    )
    (version_dir / "model.safetensors").write_text("weights", encoding="utf-8")


# ---------------------------------------------------------------------------
# Contrat de POST /train/intent
# ---------------------------------------------------------------------------

def test_start_intent_training_202_and_completion(fake_runner, intent_dataset):
    response = client.post(
        "/train/intent", json={"dataset_path": intent_dataset}, headers=HEADERS
    )
    assert response.status_code == 202, response.text
    job = response.json()
    assert job["job_id"]
    assert job["kind"] == "intent"
    # Le thread daemon démarre avant la sérialisation de la réponse : selon
    # le scheduling, le statut lu peut déjà être "running".
    assert job["status"] in ("pending", "running")

    # Attente bornée de la fin du runner simulé (thread daemon).
    from core.job_store import get_job_store

    store = get_job_store()
    deadline = time.time() + 5
    while (
        store.get(job["job_id"]).status in ("pending", "running")
        and time.time() < deadline
    ):
        time.sleep(0.05)
    stored = store.get(job["job_id"])
    assert stored.status == "completed"
    assert stored.model_path

    # Suivi par statut : le job porte bien kind="intent".
    status = client.get(f"/train/intent/status/{job['job_id']}", headers=HEADERS)
    assert status.status_code == 200
    assert status.json()["kind"] == "intent"


def test_start_intent_training_unknown_dataset_422(fake_runner):
    response = client.post(
        "/train/intent",
        json={"dataset_path": "data/does_not_exist.jsonl"},
        headers=HEADERS,
    )
    assert response.status_code == 422


def test_start_intent_training_unknown_base_version_422(fake_runner, intent_dataset):
    response = client.post(
        "/train/intent",
        json={
            "dataset_path": intent_dataset,
            "base_model_version": "20990101T000000Z-inexistant",
        },
        headers=HEADERS,
    )
    assert response.status_code == 422


def test_start_intent_training_requires_api_key(intent_dataset):
    response = client.post("/train/intent", json={"dataset_path": intent_dataset})
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Suivi / annulation
# ---------------------------------------------------------------------------

def test_status_unknown_job_404():
    response = client.get("/train/intent/status/inconnu", headers=HEADERS)
    assert response.status_code == 404


def test_cancel_unknown_job_404():
    response = client.post("/train/intent/cancel/inconnu", headers=HEADERS)
    assert response.status_code == 404


def test_cancel_running_intent_job(slow_runner, intent_dataset):
    response = client.post(
        "/train/intent", json={"dataset_path": intent_dataset}, headers=HEADERS
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    from core.job_store import get_job_store

    store = get_job_store()
    deadline = time.time() + 5
    while store.get(job_id).status != "running" and time.time() < deadline:
        time.sleep(0.05)

    cancelled = client.post(f"/train/intent/cancel/{job_id}", headers=HEADERS)
    assert cancelled.status_code == 200, cancelled.text
    body = cancelled.json()
    assert body["status"] == "cancelled"
    assert body["step"] == "cancelled"

    # Le thread simulé se termine en laissant le statut cancelled en place
    # (le runner ne réécrit pas le statut après annulation).
    deadline = time.time() + 5
    while store.get(job_id).status != "cancelled" and time.time() < deadline:
        time.sleep(0.05)
    assert store.get(job_id).status == "cancelled"


def test_jobs_listing_filters_intent_only(fake_runner, intent_dataset):
    response = client.post(
        "/train/intent", json={"dataset_path": intent_dataset}, headers=HEADERS
    )
    assert response.status_code == 202, response.text

    listing = client.get("/train/intent/jobs", headers=HEADERS)
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] >= 1
    assert body["limit"] == 100
    assert all(item["kind"] == "intent" for item in body["items"])
    assert any(
        item["job_id"] == response.json()["job_id"] for item in body["items"]
    )


# ---------------------------------------------------------------------------
# Versions / activation (dossier experiments/intent_models redirigé)
# ---------------------------------------------------------------------------

def test_versions_endpoint(tmp_path, monkeypatch):
    import core.intent_store as intent_store

    root = tmp_path / "intent_models"
    _make_version(root, "20990101T000000Z")
    monkeypatch.setattr(intent_store, "INTENT_MODEL_ROOT", str(root))

    listing = client.get("/train/intent/versions", headers=HEADERS)
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert body["items"] == ["20990101T000000Z"]
    # Pas de pointeur actif : la dernière version valide est résolue.
    assert body["active"] == "20990101T000000Z"


def test_activate_ok_writes_pointer(tmp_path, monkeypatch):
    import core.intent_store as intent_store

    root = tmp_path / "intent_models"
    _make_version(root, "20990101T000001Z")
    monkeypatch.setattr(intent_store, "INTENT_MODEL_ROOT", str(root))

    response = client.post(
        "/train/intent/activate",
        json={"version": "20990101T000001Z"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "activated", "version": "20990101T000001Z"}
    active_file = root / "active.json"
    assert (
        json.loads(active_file.read_text(encoding="utf-8"))["active"]
        == "20990101T000001Z"
    )


def test_activate_invalid_version_422(tmp_path, monkeypatch):
    import core.intent_store as intent_store

    monkeypatch.setattr(intent_store, "INTENT_MODEL_ROOT", str(tmp_path / "empty"))
    response = client.post(
        "/train/intent/activate", json={"version": "inconnu"}, headers=HEADERS
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Validation du modèle de requête + helpers légers du runner
# ---------------------------------------------------------------------------

def test_intent_train_request_validation():
    from pydantic import ValidationError

    from core.models import IntentTrainRequest

    with pytest.raises(ValidationError):
        IntentTrainRequest(epochs=0)
    with pytest.raises(ValidationError):
        IntentTrainRequest(test_size=1.5)
    with pytest.raises(ValidationError):
        IntentTrainRequest(learning_rate=-1)
    ok = IntentTrainRequest()
    assert ok.epochs == 3
    assert ok.base_model_version is None


def test_intent_steps_canonical():
    from core.models import INTENT_TRAIN_JOB_STEPS, TRAIN_JOB_STEPS

    assert INTENT_TRAIN_JOB_STEPS[0] == "queued"
    assert INTENT_TRAIN_JOB_STEPS[-1] == "done"
    assert "training" in INTENT_TRAIN_JOB_STEPS
    # L'ordre du sentiment reste inchangé (rétro-compatibilité).
    assert TRAIN_JOB_STEPS.index("augmenting_dataset") > 0


def test_load_records_and_split(intent_dataset):
    from core.intent_trainer import _load_records, _split_records

    records = _load_records(Path(intent_dataset))
    assert len(records) == 4
    assert set(records[0]) == {"text", "label"}

    train, val = _split_records(records, test_size=0.25)
    assert len(train) == 3
    assert len(val) == 1
    # Split déterministe (même graine) :
    _train2, val2 = _split_records(records, test_size=0.25)
    assert [r["text"] for r in val] == [r["text"] for r in val2]