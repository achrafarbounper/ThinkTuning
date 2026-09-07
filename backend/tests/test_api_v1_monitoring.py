# project/tests/test_api_v1_monitoring.py
"""Tests des endpoints monitoring v1 (Phase 3d-5) : /metrics (public),
/drift (auth, JSON + multipart) et /explain (auth).

Parité par construction via délégation aux handlers legacy ; les fakes
substituent uniquement les collaborateurs (predictor, agent LLM).
"""

import os

os.environ.setdefault("API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402, F401
from api import app  # noqa: E402

client = TestClient(app)
AUTH = {"X-API-Key": "test-key"}


class _FakePredictor:
    """Miroir du contrat prédiction consommé par /drift et /explain."""

    def __init__(self, mode: str = "alternating") -> None:
        self.mode = mode

    def predict(self, texts: list[str]) -> list[dict]:
        labels = ["positive" if i % 2 == 0 else "negative" for i in range(len(texts))]
        if self.mode == "all_positive":
            labels = ["positive"] * len(texts)
        return [
            {"sentiment": label, "confidence": 0.9} for label in labels
        ]


# --- /metrics (public) --------------------------------------------------------


def test_metrics_prometheus_public():
    response = client.get("/api/v1/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers.get("content-type", "")
    assert response.text.strip()  # registre prometheus_client sérialisé (non vide)


def test_metrics_json_public():
    response = client.get("/api/v1/metrics/json")
    assert response.status_code == 200
    assert response.headers.get("content-type", "").startswith("application/json")
    body = response.json()
    assert "scrape_at_ms" in body
    assert "counters" in body
    assert "histograms" in body


# --- /drift (auth) ------------------------------------------------------------


def test_drift_requires_key():
    body = {"texts_a": ["a"], "texts_b": ["b"]}
    assert client.post("/api/v1/drift", json=body).status_code == 401


def test_drift_json_kl_happy(monkeypatch):
    monkeypatch.setattr("api._get_predictor", lambda model=None: _FakePredictor())
    response = client.post(
        "/api/v1/drift",
        json={"texts_a": ["t1", "t2"], "texts_b": ["t3", "t4"]},
        headers=AUTH,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["method"] == "kl"
    assert body["drift_score"] >= 0
    assert "distribution_a" in body and "distribution_b" in body
    assert body["n_a"] == 2 and body["n_b"] == 2


def test_drift_bad_threshold_is_400(monkeypatch):
    monkeypatch.setattr("api._get_predictor", lambda model=None: _FakePredictor())
    response = client.post(
        "/api/v1/drift",
        json={"texts_a": ["a"], "texts_b": ["b"], "threshold": 0},
        headers=AUTH,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


def test_drift_multipart_status_ok(monkeypatch):
    """Le multipart (fichiers CSV) est accepté — validation montée en erreur
    400 « Deux fichiers CSV » avant prédiction (un fichier manquant)."""
    monkeypatch.setattr("api._get_predictor", lambda model=None: _FakePredictor())
    response = client.post(
        "/api/v1/drift",
        data={"text_column": "text"},
        files={"file_a": ("a.csv", b"text\nbonjour\n", "text/csv")},
        headers=AUTH,
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "bad_request"


# --- /explain (auth) -----------------------------------------------------------


def test_explain_requires_key():
    assert client.post("/api/v1/explain", json={"text": "hello"}).status_code == 401


def test_explain_happy(monkeypatch):
    monkeypatch.setattr("api._get_predictor", lambda: _FakePredictor())
    monkeypatch.setattr(
        "api.routes.explain.agent_cache.ask_agent_openrouter",
        lambda prompt, model=None: "Ce texte est positif car il exprime de la joie.",
    )
    response = client.post("/api/v1/explain", json={"text": "Super journée"}, headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["sentiment"] == "positive"
    assert body["confidence"] == 0.9
    assert "positif" in body["explanation"]
