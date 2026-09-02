# project/tests/test_model_sanity.py

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from fastapi.testclient import TestClient

import api  # noqa: F401
from api import app
from core.model_sanity import (
    SANITY_PHRASES,
    VERDICT_FALLBACK,
    VERDICT_OK,
    VERDICT_UNTRAINED,
    resolve_min_confidence,
    run_model_sanity,
)


class StubPredictor:
    """Prédicteur factice : renvoie (sentiment, confidence) de manière fixe."""

    def __init__(self, sentiment="neutral", confidence=0.333, model_path=None):
        self.sentiment = sentiment
        self.confidence = confidence
        self.model_path = model_path

    def predict(self, texts):
        return [
            {"text": t, "sentiment": self.sentiment, "confidence": self.confidence}
            for t in texts
        ]


class AlternatingPredictor(StubPredictor):
    """Se trompe sur toutes les phrases (labels faux) avec une forte
    confidence : profile « fallback base model »."""

    def predict(self, texts):
        return [
            {"text": t, "sentiment": "neutral", "confidence": 0.9} for t in texts
        ]


class GoodPredictor(StubPredictor):
    """Prédicteur sain : renvoie les labels attendus avec haute confidence."""

    def predict(self, texts):
        expected = {p["text"]: p["expected"] for p in SANITY_PHRASES}
        return [
            {"text": t, "sentiment": expected[t], "confidence": 0.95} for t in texts
        ]


# ---------------------------------------------------------------------------
# Logique de verdict (core.model_sanity)
# ---------------------------------------------------------------------------

def test_sanity_reference_phrases_covered():
    """Jeu de référence versionné : FR/EN x positif/négatif."""
    langs = {p["lang"] for p in SANITY_PHRASES}
    expected = {p["expected"] for p in SANITY_PHRASES}
    assert langs == {"fr", "en"}
    assert expected == {"positive", "negative"}
    assert len(SANITY_PHRASES) >= 8


def test_sanity_detects_untrained_model():
    """Confidence ≈ 1/3 sur toutes les classes -> verdict « untrained »."""
    report = run_model_sanity(StubPredictor("neutral", 0.333))
    assert report["status"] == "unhealthy"
    assert report["verdict"] == VERDICT_UNTRAINED
    assert report["accuracy"] == 0.0


def test_sanity_ok_with_trained_model():
    report = run_model_sanity(GoodPredictor())
    assert report["status"] == "ok"
    assert report["verdict"] == VERDICT_OK
    assert report["accuracy"] == 1.0


def test_sanity_detects_fallback_model():
    """Labels systématiquement faux -> verdict « fallback_base_model »."""
    report = run_model_sanity(AlternatingPredictor())
    assert report["verdict"] == VERDICT_FALLBACK
    assert report["status"] == "unhealthy"


def test_sanity_custom_threshold():
    """Seuil configurable : avec 0.99, même un bon prédicteur est rejeté."""
    report = run_model_sanity(GoodPredictor(), min_confidence=0.99)
    assert report["verdict"] == VERDICT_UNTRAINED
    assert report["min_confidence"] == 0.99


def test_resolve_min_confidence_env_override(monkeypatch):
    monkeypatch.setenv("MODEL_SANITY_MIN_CONFIDENCE", "0.55")
    assert resolve_min_confidence() == 0.55


def test_resolve_min_confidence_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv("MODEL_SANITY_MIN_CONFIDENCE", "not-a-float")
    assert resolve_min_confidence() == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# Endpoint GET /health/model-sanity
# ---------------------------------------------------------------------------

client = TestClient(app)


def test_health_model_sanity_ok(monkeypatch):
    monkeypatch.setattr(
        "api.routes.health.get_predictor", lambda model=None: GoodPredictor()
    )
    response = client.get("/health/model-sanity")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verdict"] == "ok"
    assert body["status"] == "ok"
    assert len(body["results"]) == len(SANITY_PHRASES)


def test_health_model_sanity_untrained_returns_503(monkeypatch):
    monkeypatch.setattr(
        "api.routes.health.get_predictor",
        lambda model=None: StubPredictor("neutral", 0.333),
    )
    response = client.get("/health/model-sanity")
    assert response.status_code == 503, response.text
    body = response.json()["detail"]
    assert body["verdict"] == "untrained"
    assert body["status"] == "unhealthy"
    assert "non entraîné" in body["detail"]


def test_health_model_sanity_fallback_returns_503(monkeypatch):
    monkeypatch.setattr(
        "api.routes.health.get_predictor", lambda model=None: AlternatingPredictor()
    )
    response = client.get("/health/model-sanity")
    assert response.status_code == 503, response.text
    body = response.json()["detail"]
    assert body["verdict"] == "fallback_base_model"


def test_health_model_sanity_with_model_name_param(monkeypatch):
    """Le paramètre `model_name` est transmis au résolveur de modèle."""
    received = {}

    def fake_get_predictor(model_name=None):
        received["model_name"] = model_name
        return GoodPredictor()

    monkeypatch.setattr("api.routes.health.get_predictor", fake_get_predictor)
    response = client.get(
        "/health/model-sanity", params={"model_name": "20260101T000000Z"}
    )
    assert response.status_code == 200, response.text
    assert received["model_name"] == "20260101T000000Z"
    assert response.json()["model"] == "20260101T000000Z"


def test_health_model_sanity_without_model_name_defaults_to_active(monkeypatch):
    """Sans `model_name`, la version active est utilisée (model_name=None)."""
    received = {}

    def fake_get_predictor(model_name=None):
        received["model_name"] = model_name
        return GoodPredictor()

    monkeypatch.setattr("api.routes.health.get_predictor", fake_get_predictor)
    response = client.get("/health/model-sanity")
    assert response.status_code == 200, response.text
    assert received["model_name"] is None
    assert response.json()["model"] is None


def test_health_model_sanity_unknown_model_returns_503(monkeypatch):
    """Une version inexistante -> 503 avec verdict `model_unavailable`."""
    from fastapi import HTTPException as _HTTPException

    def fake_get_predictor(model_name=None):
        raise _HTTPException(status_code=503, detail="Model version 'nope' not found.")

    monkeypatch.setattr("api.routes.health.get_predictor", fake_get_predictor)
    response = client.get("/health/model-sanity", params={"model_name": "nope"})
    assert response.status_code == 503, response.text


# ---------------------------------------------------------------------------
# POST /predict/reload : sanity check après rechargement
# ---------------------------------------------------------------------------

def test_reload_rejects_broken_model(monkeypatch):
    monkeypatch.setattr("api.routes.predict.reload_predictor", lambda: None)
    monkeypatch.setattr(
        "api._get_predictor", lambda model=None: StubPredictor("neutral", 0.333)
    )
    response = client.post(
        "/predict/reload", headers={"X-API-Key": os.environ["API_KEY"]}
    )
    assert response.status_code == 503, response.text
    body = response.json()["detail"]
    assert body["status"] == "reload_rejected"
    assert body["verdict"] == "untrained"


def test_reload_accepts_healthy_model(monkeypatch):
    monkeypatch.setattr("api.routes.predict.reload_predictor", lambda: None)
    monkeypatch.setattr("api._get_predictor", lambda model=None: GoodPredictor())
    response = client.post(
        "/predict/reload", headers={"X-API-Key": os.environ["API_KEY"]}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"status": "reloaded", "sanity": "ok"}

