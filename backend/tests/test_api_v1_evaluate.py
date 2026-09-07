# project/tests/test_api_v1_evaluate.py
"""Tests de l'endpoint d'évaluation v1 (Phase 3d-3).

Deux niveaux :
    - route : port substitué (aucun modèle chargé, aucune donnée réelle) ;
    - adaptateur : handler legacy ``confusion_route`` enveloppé avec
      ``api._get_predictor`` / ``api.load_raw_dataset`` monkeypatchés —
      vérifie la conversion 503 -> ``ModelNotAvailableError`` et le calcul
      sklearn de bout en bout (petit échantillon déterministe).
"""

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import api  # noqa: F401
from api import app
from api.dependencies.composition import get_evaluation_port
from app.domain.errors import ModelNotAvailableError, ValidationError
from app.infrastructure.ml.model_versioning_adapter import ModuleEvaluationAdapter

client = TestClient(app)

AUTH = {"X-API-Key": "test-key"}


# ---------------------------------------------------------------------------
# Fake du port (aucune infrastructure)
# ---------------------------------------------------------------------------
class FakeEvaluation:
    def __init__(self) -> None:
        self.result: dict = {}
        self.error: Exception | None = None
        self.calls: list[dict] = []

    def run_confusion(self, *, model, limit, max_mistakes) -> dict:
        self.calls.append({"model": model, "limit": limit, "max_mistakes": max_mistakes})
        if self.error is not None:
            raise self.error
        return self.result


@pytest.fixture
def fake_evaluation():
    fake = FakeEvaluation()
    app.dependency_overrides[get_evaluation_port] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_evaluation_port, None)


# ---------------------------------------------------------------------------
# GET /api/v1/evaluate/confusion (route)
# ---------------------------------------------------------------------------
def test_v1_evaluate_confusion_200(fake_evaluation):
    fake_evaluation.result = {
        "model": None,
        "n": 2,
        "labels": ["negative", "neutral", "positive"],
        "matrix": [[1, 0, 0], [0, 0, 0], [0, 0, 1]],
        "metrics": {"accuracy": 1.0, "f1_macro": 1.0, "per_class_recall": {}},
        "errors_by_class": [],
        "mistakes": [],
        "confusion_pairs": [],
    }

    response = client.get("/api/v1/evaluate/confusion", headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.json()["n"] == 2
    assert response.json()["matrix"] == [[1, 0, 0], [0, 0, 0], [0, 0, 1]]


def test_v1_evaluate_confusion_query_passthrough(fake_evaluation):
    """model/limit/max_mistakes sont transmis tels quels au use case."""
    response = client.get(
        "/api/v1/evaluate/confusion?model=20260905&limit=50&max_mistakes=10",
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    assert fake_evaluation.calls == [
        {"model": "20260905", "limit": 50, "max_mistakes": 10}
    ]


def test_v1_evaluate_confusion_requires_api_key():
    response = client.get("/api/v1/evaluate/confusion")

    assert response.status_code == 401, response.text


def test_v1_evaluate_confusion_no_model_503(fake_evaluation):
    """Aucun modèle exploitable : 503 enveloppe domaine (même contrat /predict)."""
    fake_evaluation.error = ModelNotAvailableError("Aucun modèle disponible")

    response = client.get("/api/v1/evaluate/confusion", headers=AUTH)

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "model_not_available"


def test_v1_evaluate_confusion_empty_sample_422(fake_evaluation):
    fake_evaluation.error = ValidationError("Échantillon de référence vide.")

    response = client.get("/api/v1/evaluate/confusion", headers=AUTH)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
# Adaptateur réel : confusion_route legacy + monkeypatchs api.*
# ---------------------------------------------------------------------------
class FakePredictor:
    """Prédicteur déterministe : alternance positive/negative (ids 2/0)."""

    def predict(self, texts):
        out = []
        for i, _text in enumerate(texts):
            sentiment = "positive" if i % 2 == 0 else "negative"
            out.append({"sentiment": sentiment, "confidence": 0.9})
        return out


def test_adapter_confusion_computes_matrix(monkeypatch):
    """Sklearn de bout en bout sur un échantillon minimal (2 ex. : 1 ok, 1 ko)."""
    monkeypatch.setattr(
        api,
        "load_raw_dataset",
        lambda **kwargs: {"text": ["a", "b"], "label": [2, 2]},
    )
    monkeypatch.setattr(api, "_get_predictor", lambda model=None: FakePredictor())
    adapter = ModuleEvaluationAdapter()

    result = adapter.run_confusion(model=None, limit=10, max_mistakes=5)

    # "a" prédit positive (2) = correct ; "b" prédit negative (0) = erreur.
    assert result["n"] == 2
    assert result["labels"] == ["negative", "neutral", "positive"]
    assert result["matrix"][2][2] == 1
    assert result["matrix"][2][0] == 1
    assert result["metrics"]["accuracy"] == 0.5
    assert len(result["mistakes"]) == 1
    assert result["mistakes"][0]["true_label"] == "positive"
    assert result["mistakes"][0]["pred_label"] == "negative"
    assert result["confusion_pairs"] == [
        {"true_label": "positive", "pred_label": "negative", "count": 1}
    ]


def test_adapter_confusion_no_model_503_conversion(monkeypatch):
    """HTTPException 503 de _get_predictor => ModelNotAvailableError."""
    def _no_model(model=None):
        raise HTTPException(status_code=503, detail="Aucun modèle disponible")

    monkeypatch.setattr(api, "_get_predictor", _no_model)
    adapter = ModuleEvaluationAdapter()

    with pytest.raises(ModelNotAvailableError):
        adapter.run_confusion(model=None, limit=10, max_mistakes=5)


def test_adapter_confusion_empty_sample_422_conversion(monkeypatch):
    """Échantillon vide => HTTPException 422 => ValidationError."""
    monkeypatch.setattr(
        api, "load_raw_dataset", lambda **kwargs: {"text": [], "label": []}
    )
    monkeypatch.setattr(api, "_get_predictor", lambda model=None: FakePredictor())
    adapter = ModuleEvaluationAdapter()

    with pytest.raises(ValidationError):
        adapter.run_confusion(model=None, limit=10, max_mistakes=5)
