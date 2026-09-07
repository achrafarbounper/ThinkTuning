# project/tests/test_api_v1_active_learning.py
"""Tests des endpoints active learning / annotate v1 (Phase 3d-5).

Parité par construction via délégation aux handlers legacy ; fakes du store
d'annotations et du job store (aucun cycle réel lancé).
"""

import os

os.environ.setdefault("API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402, F401
from api import app  # noqa: E402
from core.models import TrainJob

client = TestClient(app)
AUTH = {"X-API-Key": "test-key"}


class _FakeAnnotationStore:
    """Miroir du contrat HTTP de core.annotation_store.AnnotationStore."""

    def __init__(self) -> None:
        self.items: list[dict] = []

    def annotate(self, text: str, label: str, force: bool = False) -> dict:
        if not text.strip():
            raise ValueError("texte vide")
        row = {"text": text, "label": label}
        self.items.append(row)
        return row

    def count(self) -> int:
        return len(self.items)

    def list(self, limit: int = 100, offset: int = 0) -> list[dict]:
        return self.items[offset : offset + limit]

    def merge_annotations(self, output_path=None) -> dict:
        if not self.items:
            raise RuntimeError("Aucune annotation à fusionner")
        return {"annotations_merged": len(self.items)}


class _FakeJobStore:
    def __init__(self) -> None:
        self.jobs: dict[str, TrainJob] = {}

    def __setitem__(self, job_id: str, job: TrainJob) -> None:
        self.jobs[job_id] = job

    def get(self, job_id: str) -> TrainJob | None:
        return self.jobs.get(job_id)


def _install_fakes(monkeypatch):
    ann = _FakeAnnotationStore()
    jobs = _FakeJobStore()
    monkeypatch.setattr("api.routes.active_learning.get_annotation_store", lambda: ann)
    monkeypatch.setattr("api.routes.active_learning.get_job_store", lambda: jobs)
    monkeypatch.setattr("api.routes.active_learning.run_cycle", lambda *a, **k: None)
    return ann, jobs


def test_active_learning_requires_key(monkeypatch):
    assert client.post("/api/v1/active_learning", json={"texts": ["a"]}).status_code == 401


def test_select_examples_happy(monkeypatch):
    _install_fakes(monkeypatch)
    monkeypatch.setattr(
        "active_learning.select_uncertain_examples",
        lambda texts, model_path=None, batch_size=32, top_n=50: [
            {"text": t, "predicted_label": "neutral", "confidence": 0.33, "uncertainty": 0.67}
            for t in texts
        ],
    )
    response = client.post(
        "/api/v1/active_learning", json={"texts": ["t1", "t2"]}, headers=AUTH
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["items"][0]["text"] == "t1"


def test_select_examples_empty_is_400(monkeypatch):
    _install_fakes(monkeypatch)
    # ``texts=[]`` est falsy : le handler retombe sur le dataset par défaut.
    # On force ``_load_texts`` à ne rien charger pour atteindre la branche 400.
    monkeypatch.setattr("api.routes.active_learning._load_texts", lambda path, texts: [])
    response = client.post("/api/v1/active_learning", json={"texts": []}, headers=AUTH)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_annotate_happy(monkeypatch):
    _install_fakes(monkeypatch)
    response = client.post(
        "/api/v1/annotate", json={"text": "superbe", "label": "positive"}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json()["label"] == "positive"


def test_annotate_empty_text_is_422(monkeypatch):
    _install_fakes(monkeypatch)
    response = client.post("/api/v1/annotate", json={"text": "", "label": "positive"}, headers=AUTH)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_list_annotations(monkeypatch):
    ann, _ = _install_fakes(monkeypatch)
    ann.annotate("bonjour", "positive")
    response = client.get("/api/v1/annotate/list", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["text"] == "bonjour"


def test_merge_annotations_happy(monkeypatch):
    ann, _ = _install_fakes(monkeypatch)
    ann.annotate("bonjour", "positive")
    response = client.post("/api/v1/annotate/merge", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["stats"]["annotations_merged"] == 1


def test_start_cycle_202_and_status(monkeypatch):
    _, jobs = _install_fakes(monkeypatch)
    started = client.post("/api/v1/active_learning/cycle", json={}, headers=AUTH)
    assert started.status_code == 202
    job_id = started.json()["job_id"]
    assert job_id in jobs.jobs

    status = client.get(f"/api/v1/active_learning/cycle/status/{job_id}", headers=AUTH)
    assert status.status_code == 200
    assert status.json()["job_id"] == job_id

    missing = client.get("/api/v1/active_learning/cycle/status/nope", headers=AUTH)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
