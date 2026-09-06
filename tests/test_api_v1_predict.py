# project/tests/test_api_v1_predict.py
"""Tests des endpoints de prédiction v1 (POST /api/v1/predict*, découplage).

Les fakes substituent le PredictionPort via ``app.dependency_overrides``
(pattern officiel FastAPI) : AUCUN modèle réel n'est chargé (coût CI) et les
règles métier du use-case (refus d'un modèle non sain au reload) sont
validées de façon déterministe.
"""

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from fastapi.testclient import TestClient

import api  # noqa: F401
from api import app
from api.dependencies.composition import get_prediction_port
from app.domain.entities.prediction import PredictionResult, SanityReport
from app.domain.errors import ModelNotAvailableError

client = TestClient(app)

AUTH = {"X-API-Key": "test-key"}


class FakePredictor:
    """PredictionPort déterministe (aucune dépendance Transformers)."""

    def __init__(
        self,
        *,
        fail: bool = False,
        sanity_ok: bool = True,
        sentiment: str = "positive",
        confidence: float = 0.9,
    ) -> None:
        self.fail = fail
        self.sanity_ok = sanity_ok
        self.sentiment = sentiment
        self.confidence = confidence
        self.predicted: list[str] = []

    def predict(self, texts: list[str], model_name: str | None = None):
        if self.fail:
            raise ModelNotAvailableError("Aucun modèle disponible")
        self.predicted.extend(texts)
        return [
            PredictionResult(
                text=t,
                sentiment=self.sentiment,
                confidence=self.confidence,
                model_version="v-test",
            )
            for t in texts
        ]

    def sanity_check(self, model_name: str | None = None) -> SanityReport:
        return SanityReport(
            verdict="ok" if self.sanity_ok else "untrained",
            ok=self.sanity_ok,
            status="ok" if self.sanity_ok else "unhealthy",
            detail="" if self.sanity_ok else "Modèle non entraîné",
            min_confidence=0.4,
            accuracy=1.0 if self.sanity_ok else 0.25,
        )

    def reload(self) -> SanityReport:
        return self.sanity_check()


@pytest.fixture
def fake_predictor():
    """Substitue le port d'inférence le temps d'un test (auto-nettoyage)."""
    fake = FakePredictor()
    app.dependency_overrides[get_prediction_port] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_prediction_port, None)


def test_v1_predict_single_text(fake_predictor):
    """Un texte => une prédiction, ordre et shape v1 (text/sentiment/confidence)."""
    response = client.post(
        "/api/v1/predict", json={"texts": ["Service impeccable"]}, headers=AUTH
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model_version"] == "v-test"
    assert body["results"] == [
        {
            "text": "Service impeccable",
            "sentiment": "positive",
            "confidence": 0.9,
            "model_version": "v-test",
        }
    ]
    assert fake_predictor.predicted == ["Service impeccable"]


def test_v1_predict_preserves_order(fake_predictor):
    """Lot multi-phrases : l'ordre d'entrée est préservé (contrat legacy)."""
    texts = ["a", "b", "c"]

    response = client.post("/api/v1/predict", json={"texts": texts}, headers=AUTH)

    assert [r["text"] for r in response.json()["results"]] == texts


def test_v1_predict_requires_api_key(fake_predictor):
    """Surface v1 protégée : sans X-API-Key => 401 (posture production)."""
    response = client.post("/api/v1/predict", json={"texts": ["x"]})

    assert response.status_code == 401, response.text


def test_v1_predict_model_unavailable():
    """Modèle absent : 503 + payload domaine ({"error": {"code", ...}})."""
    app.dependency_overrides[get_prediction_port] = lambda: FakePredictor(fail=True)
    try:
        response = client.post(
            "/api/v1/predict", json={"texts": ["x"]}, headers=AUTH
        )
    finally:
        app.dependency_overrides.pop(get_prediction_port, None)

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "model_not_available"


def test_v1_predict_rejects_empty_list(fake_predictor):
    """Liste vide : 422 (évite le crash tokenizer legacy, garde-fou anti-DoS)."""
    response = client.post("/api/v1/predict", json={"texts": []}, headers=AUTH)

    assert response.status_code == 422, response.text


def test_v1_predict_rejects_oversized_batch(fake_predictor):
    """Au-delà de PREDICT_MAX_TEXTS (256 défaut) : 422, l'API ne charge pas."""
    response = client.post(
        "/api/v1/predict", json={"texts": ["x"] * 257}, headers=AUTH
    )

    assert response.status_code == 422, response.text


def test_v1_reload_healthy_model(fake_predictor):
    """Reload + modèle sain : 200, shape legacy préservé {"status", "sanity"}."""
    response = client.post("/api/v1/predict/reload", headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "reloaded", "sanity": "ok"}


def test_v1_reload_rejects_unhealthy_model():
    """SCRUM-74 : un rechargement vers un modèle non entraîné est REFUSÉ (503)."""
    app.dependency_overrides[get_prediction_port] = lambda: FakePredictor(sanity_ok=False)
    try:
        response = client.post("/api/v1/predict/reload", headers=AUTH)
    finally:
        app.dependency_overrides.pop(get_prediction_port, None)

    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "model_unhealthy"
    assert error["details"]["status"] == "reload_rejected"


def test_v1_predict_rate_limited(fake_predictor, monkeypatch):
    """Parité anti-DoS : /api/v1/predict est couvert par le token bucket.

    Mêmes règles que /predict legacy (Retry-After, 429) : une limite absente
    ou distincte pour la v1 créerait un contournement trivial pendant la
    migration.
    """
    monkeypatch.setattr(api, "RATE_LIMIT_PER_MINUTE", 1)
    api._reset_rate_limit_buckets()
    try:
        first = client.post("/api/v1/predict", json={"texts": ["a"]}, headers=AUTH)
        second = client.post("/api/v1/predict", json={"texts": ["b"]}, headers=AUTH)
    finally:
        api._reset_rate_limit_buckets()

    assert first.status_code == 200, first.text
    assert second.status_code == 429, second.text
    assert second.headers.get("Retry-After", "").isdigit()


# -- /predict/batch (multipart CSV, délégation legacy — Phase « strangler complet »)

BATCH_CSV = "text\nService impeccable\nJe ne reviendrai pas\n"


class FakeLegacyPredictor:
    """Fake du prédicteur legacy (``api._get_predictor``) — dicte les même
    shape que le handler : ``[{"sentiment", "confidence"}]``."""

    def predict(self, texts: list[str]):
        return [{"sentiment": "positive", "confidence": 0.9} for _ in texts]


@pytest.fixture
def fake_legacy_predictor(monkeypatch):
    fake = FakeLegacyPredictor()
    monkeypatch.setattr(api, "_get_predictor", lambda model=None: fake)
    return fake


def test_v1_predict_batch_json(fake_legacy_predictor):
    """CSV uploadé => résultats JSON (délégation : ordre et shape legacy)."""
    response = client.post(
        "/api/v1/predict/batch",
        files={"file": ("t.csv", BATCH_CSV.encode(), "text/csv")},
        data={"text_column": "text", "response_format": "json"},
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert len(results) == 2
    assert results[0]["text"] == "Service impeccable"
    assert results[0]["sentiment"] == "positive"
    assert results[0]["confidence"] == 0.9
    assert results[0]["row_index"] == 0


def test_v1_predict_batch_csv(fake_legacy_predictor):
    """response_format=csv => flux CSV (Content-Disposition préservée)."""
    response = client.post(
        "/api/v1/predict/batch",
        files={"file": ("t.csv", BATCH_CSV.encode(), "text/csv")},
        data={"text_column": "text", "response_format": "csv"},
        headers=AUTH,
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    assert "sentiment" in response.text
    assert "filename=predictions.csv" in response.headers["content-disposition"]


def test_v1_predict_batch_invalid_csv(fake_legacy_predictor):
    """CSV illisible => enveloppe d'erreur v1 (code stable), pas de 500."""
    response = client.post(
        "/api/v1/predict/batch",
        files={"file": ("bad.csv", b"not a csv \xff\xfe", "text/csv")},
        data={"text_column": "text", "response_format": "json"},
        headers=AUTH,
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "bad_request"


def test_v1_predict_batch_missing_column(fake_legacy_predictor):
    """Colonne introuvable => refus explicite (comportement legacy conservé)."""
    response = client.post(
        "/api/v1/predict/batch",
        files={"file": ("t.csv", b"tweet\nhello\n", "text/csv")},
        data={"text_column": "text", "response_format": "json"},
        headers=AUTH,
    )

    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "bad_request"


def test_v1_predict_batch_requires_api_key():
    """Parité auth : le batch (upload coûteux) exige la clé API."""
    response = client.post(
        "/api/v1/predict/batch",
        files={"file": ("t.csv", BATCH_CSV.encode(), "text/csv")},
        data={"text_column": "text", "response_format": "json"},
    )

    assert response.status_code == 401, response.text


def test_v1_predict_batch_rate_limited(fake_legacy_predictor, monkeypatch):
    """Anti-DoS partagé : /api/v1/predict/batch est dans le même token bucket."""
    monkeypatch.setattr(api, "RATE_LIMIT_PER_MINUTE", 1)
    api._reset_rate_limit_buckets()
    try:
        first = client.post(
            "/api/v1/predict/batch",
            files={"file": ("t.csv", BATCH_CSV.encode(), "text/csv")},
            data={"text_column": "text", "response_format": "json"},
            headers=AUTH,
        )
        second = client.post(
            "/api/v1/predict/batch",
            files={"file": ("t.csv", BATCH_CSV.encode(), "text/csv")},
            data={"text_column": "text", "response_format": "json"},
            headers=AUTH,
        )
    finally:
        api._reset_rate_limit_buckets()

    assert first.status_code == 200, first.text
    assert second.status_code == 429, second.text
