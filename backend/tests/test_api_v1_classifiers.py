# project/tests/test_api_v1_classifiers.py
"""Tests des endpoints classifiers v1 (Phase 3d-5).

Posture d'auth DECLINÉE (GET publics, POST protégés — parité legacy).
Les fakes substituent le résolveur/registry : aucun classifieur réel chargé.
"""

import os

os.environ.setdefault("API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402, F401
from api import app  # noqa: E402

client = TestClient(app)
AUTH = {"X-API-Key": "test-key"}


class _FakePrediction:
    def __init__(self, text: str) -> None:
        self._text = text
        self.label = "positive"
        self.confidence = 0.95
        self.probabilities = {"positive": 0.95, "neutral": 0.05}

    def to_dict(self) -> dict:
        return {
            "text": self._text,
            "label": self.label,
            "confidence": self.confidence,
            "probabilities": self.probabilities,
        }


class _FakeClassifier:
    def predict(self, texts: list[str]) -> list[_FakePrediction]:
        return [_FakePrediction(t) for t in texts]

    def reload(self) -> None:
        pass

    def get_model_info(self) -> dict:
        return {"name": "sentiment", "status": "ready"}


# --- GET (publics) ------------------------------------------------------------


def test_list_classifiers_public(monkeypatch):
    monkeypatch.setattr(
        "api.routes.classifiers.classifier_snapshots",
        lambda registry: [{"name": "sentiment", "status": "healthy"}],
    )
    monkeypatch.setattr(
        "api.routes.classifiers.health_summary",
        lambda snapshots: {"total": 1, "healthy": 1, "status": "ok"},
    )
    monkeypatch.setattr("api.routes.classifiers.get_registry", lambda: object())
    response = client.get("/api/v1/classifiers")
    assert response.status_code == 200
    body = response.json()
    assert body["classifiers"][0]["name"] == "sentiment"
    assert body["summary"]["status"] == "ok"


def test_get_classifier_public_and_missing(monkeypatch):
    monkeypatch.setattr(
        "api.routes.classifiers.classifier_snapshot",
        lambda name, classifier: {"name": name, "status": "healthy"},
    )
    monkeypatch.setattr("api.routes.classifiers._resolve_classifier", lambda name: object())
    response = client.get("/api/v1/classifiers/sentiment")
    assert response.status_code == 200
    assert response.json()["name"] == "sentiment"

    # Classifieur inconnu : registry réel vide + aucune fabrique pour « bogus ».
    monkeypatch.setattr("api.routes.classifiers._resolve_classifier", lambda name: _missing(name))
    missing = client.get("/api/v1/classifiers/bogus")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


def _missing(name: str):
    from fastapi import HTTPException

    raise HTTPException(status_code=404, detail=f"Classifieur inconnu : {name!r}")


# --- POST predict / reload (protégés) -----------------------------------------


def test_predict_requires_key():
    response = client.post("/api/v1/classifiers/sentiment/predict", json={"texts": ["x"]})
    assert response.status_code == 401


def test_predict_classifier_happy(monkeypatch):
    monkeypatch.setattr(
        "api.routes.classifiers._resolve_classifier", lambda name: _FakeClassifier()
    )
    response = client.post(
        "/api/v1/classifiers/sentiment/predict",
        json={"texts": ["bonjour", "dégueulasse"]},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["results"]) == 2
    assert body["results"][0]["label"] == "positive"
    assert "probabilities" in body["results"][0]


def test_predict_failure_is_500(monkeypatch):
    class _Exploding:
        def predict(self, texts):
            raise RuntimeError("panne interne")

    monkeypatch.setattr("api.routes.classifiers._resolve_classifier", lambda name: _Exploding())
    response = client.post(
        "/api/v1/classifiers/sentiment/predict",
        json={"texts": ["x"]},
        headers=AUTH,
    )
    assert response.status_code == 500


def test_reload_requires_key():
    assert client.post("/api/v1/classifiers/sentiment/reload").status_code == 401


def test_reload_classifier_happy(monkeypatch):
    monkeypatch.setattr(
        "api.routes.classifiers._resolve_classifier", lambda name: _FakeClassifier()
    )
    response = client.post("/api/v1/classifiers/sentiment/reload", headers=AUTH)
    assert response.status_code == 200
    assert response.json()["status"] == "reloaded"
    assert response.json()["model_info"]["name"] == "sentiment"


def test_reload_unknown_is_404(monkeypatch):
    monkeypatch.setattr("api.routes.classifiers._resolve_classifier", _missing)
    response = client.post("/api/v1/classifiers/bogus/reload", headers=AUTH)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
