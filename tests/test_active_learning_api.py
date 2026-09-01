# project/tests/test_active_learning_api.py

"""Tests des routes du cycle Active Learning (SCRUM-55)."""

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from fastapi.testclient import TestClient

import api  # noqa: F401
from api import app
from core.annotation_store import get_annotation_store

HEADERS = {"X-API-Key": "test-key"}
client = TestClient(app)


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    """Redirige le journal d'annotations vers un fichier temporaire."""
    path = str(tmp_path / "annotations.jsonl")
    monkeypatch.setenv("ANNOTATIONS_PATH", path)
    # Le singleton est deja cree (import) : on le remplace pour le test.
    import core.annotation_store as mod

    store = mod.AnnotationStore(path)
    monkeypatch.setattr(mod, "_store_singleton", store)
    monkeypatch.setattr("api.routes.active_learning.get_annotation_store", lambda: store)
    return store


def test_annotate_ok_and_list(isolated_store):
    response = client.post(
        "/annotate",
        json={"text": "Service impeccable", "label": "positif"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    record = response.json()
    assert record["label"] == 2

    listing = client.get("/annotate/list", headers=HEADERS)
    assert listing.status_code == 200
    body = listing.json()
    assert body["total"] == 1
    assert body["items"][0]["text"] == "Service impeccable"


def test_annotate_rejects_invalid_label(isolated_store):
    response = client.post(
        "/annotate",
        json={"text": "texte", "label": "joie"},
        headers=HEADERS,
    )
    assert response.status_code == 422


def test_annotate_deduplicates(isolated_store):
    for label in ("negatif", "negative"):
        response = client.post(
            "/annotate",
            json={"text": "Service nul", "label": label},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text
    listing = client.get("/annotate/list", headers=HEADERS)
    assert listing.json()["total"] == 1


def test_cycle_requires_annotations(isolated_store):
    """Sans annotation, le job de cycle doit echouer proprement (pas de crash)."""
    response = client.post("/active_learning/cycle", json={}, headers=HEADERS)
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    import time

    from core.job_store import get_job_store
    from core.models import JobStatus

    store = get_job_store()
    job = store.get(job_id)
    assert job is not None
    # Le cycle tourne dans un thread daemon : attente active bornee.
    deadline = time.time() + 10
    while job.status in (JobStatus.PENDING, JobStatus.RUNNING) and time.time() < deadline:
        time.sleep(0.1)
    assert job.status == JobStatus.FAILED
    assert "aucune annotation" in (job.error or "")


def test_active_endpoint_requires_api_key(isolated_store):
    response = client.post("/active_learning", json={"texts": ["hello"]})
    assert response.status_code in (401, 403)
