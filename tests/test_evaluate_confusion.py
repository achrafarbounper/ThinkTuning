"""
Tests de l'endpoint GET /evaluate/confusion (page « Évaluation » du dashboard).

Totalement offline : predictor et dataset sont remplacés par des doubles
locaux. Vérifie la matrice de confusion, les erreurs par classe, les métriques
et le cas « aucun modèle disponible -> 503 ».
"""

import os

os.environ.setdefault("API_KEY", "test-key")

from datasets import Dataset
from fastapi import HTTPException
from fastapi.testclient import TestClient

import api
from api import app

client = TestClient(app)

# Sentiments prédits par le fake predictor, indexés par texte.
PRED_MAP = {
    "a": "negative",
    "b": "negative",
    "c": "neutral",
    "d": "positive",
    "e": "negative",
}


class FakePredictor:
    """Predictor déterministe : retourne PRED_MAP[t] pour chaque texte."""

    def predict(self, texts):
        return [
            {"text": t, "sentiment": PRED_MAP[t], "confidence": 0.9}
            for t in texts
        ]


def _mock_dataset_and_predictor(monkeypatch, labels=None, predictor=None):
    if labels is None:
        labels = [0, 0, 1, 2, 2]
    texts = ["a", "b", "c", "d", "e"]
    ds = Dataset.from_dict({"text": texts, "label": labels, "lang_code": ["fr"] * len(texts)})
    monkeypatch.setattr(api, "load_raw_dataset", lambda **kwargs: ds)
    monkeypatch.setattr(api, "_get_predictor", lambda model=None: predictor or FakePredictor())
    return texts


def test_confusion_route_shape_and_matrix(monkeypatch):
    _mock_dataset_and_predictor(monkeypatch)

    response = client.get("/evaluate/confusion", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["labels"] == ["negative", "neutral", "positive"]
    assert payload["n"] == 5
    assert len(payload["matrix"]) == 3
    assert all(len(row) == 3 for row in payload["matrix"])

    # Lignes = vrai, colonnes = prédit.
    # PRED_MAP -> preds ids [0, 0, 1, 2, 0] pour labels [0, 0, 1, 2, 2].
    assert payload["matrix"] == [[2, 0, 0], [0, 1, 0], [1, 0, 1]]

    assert payload["metrics"]["accuracy"] == 0.8
    assert isinstance(payload["metrics"]["f1_macro"], float)

    assert payload["errors_by_class"] == [
        {"label": "negative", "total": 2, "correct": 2, "errors": 0},
        {"label": "neutral", "total": 1, "correct": 1, "errors": 0},
        {"label": "positive", "total": 2, "correct": 1, "errors": 1},
    ]


def test_confusion_route_forwards_model_param(monkeypatch):
    captured = {"value": None}

    ds = Dataset.from_dict({"text": ["a", "b", "c"], "label": [0, 1, 2], "lang_code": ["fr"] * 3})
    monkeypatch.setattr(api, "load_raw_dataset", lambda **kwargs: ds)

    def fake_get_predictor(model=None):
        captured["value"] = model
        return FakePredictor()

    monkeypatch.setattr(api, "_get_predictor", fake_get_predictor)
    response = client.get(
        "/evaluate/confusion?model=vintage-2024",
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 200, response.text
    assert captured["value"] == "vintage-2024"


def test_confusion_route_mistakes_and_confusion_pairs(monkeypatch):
    _mock_dataset_and_predictor(monkeypatch)

    response = client.get("/evaluate/confusion", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200, response.text
    payload = response.json()

    # Une seule erreur : « e » (vrai=positive, prédit=negative).
    mistakes = payload["mistakes"]
    assert isinstance(mistakes, list)
    assert len(mistakes) == 1
    m = mistakes[0]
    assert m["true_label"] == "positive"
    assert m["pred_label"] == "negative"
    assert m["text"] == "e"
    assert m["confidence"] == 0.9

    # Paires de confusion : correspondance matrice[positive][negative] = 1.
    pairs = payload["confusion_pairs"]
    assert pairs == [{"true_label": "positive", "pred_label": "negative", "count": 1}]


def test_confusion_route_perfect_prediction_empty_mistakes(monkeypatch):
    # Predictor exact : PRED[ texte ] == son label réel partout -> aucune erreur.
    class PerfectPredictor:
        def predict(self, texts):
            map_pred = {"a": "negative", "b": "neutral", "c": "positive"}
            return [
                {"text": t, "sentiment": map_pred[t], "confidence": 0.99}
                for t in texts
            ]

    ds = Dataset.from_dict({
        "text": ["a", "b", "c"],
        "label": [0, 1, 2],
        "lang_code": ["fr"] * 3,
    })
    monkeypatch.setattr(api, "load_raw_dataset", lambda **kwargs: ds)
    monkeypatch.setattr(api, "_get_predictor", lambda model=None: PerfectPredictor())

    response = client.get("/evaluate/confusion", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["metrics"]["accuracy"] == 1.0
    assert payload["mistakes"] == []
    assert payload["confusion_pairs"] == []
    assert payload["errors_by_class"] == [
        {"label": "negative", "total": 1, "correct": 1, "errors": 0},
        {"label": "neutral", "total": 1, "correct": 1, "errors": 0},
        {"label": "positive", "total": 1, "correct": 1, "errors": 0},
    ]


def test_confusion_route_max_mistakes_limits(monkeypatch):
    # 3 erreurs potentielles mais max_mistakes=1 -> un seul exemple renvoyé.
    class ManyMistakesPredictor:
        def predict(self, texts):
            # Prédit toujours "negative" -> erreur pour les labels 1 et 2.
            return [{"text": t, "sentiment": "negative", "confidence": 0.8} for t in texts]

    ds = Dataset.from_dict({
        "text": ["a", "b", "c"],
        "label": [0, 1, 2],
        "lang_code": ["fr"] * 3,
    })
    monkeypatch.setattr(api, "load_raw_dataset", lambda **kwargs: ds)
    monkeypatch.setattr(api, "_get_predictor", lambda model=None: ManyMistakesPredictor())

    response = client.get(
        "/evaluate/confusion?max_mistakes=1",
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert len(payload["mistakes"]) == 1


def test_confusion_route_no_model_503(monkeypatch):
    def fake_get_predictor(model=None):
        raise HTTPException(status_code=503, detail="Aucun modèle disponible")

    monkeypatch.setattr(api, "_get_predictor", fake_get_predictor)

    response = client.get("/evaluate/confusion", headers={"X-API-Key": "test-key"})
    assert response.status_code == 503, response.text


def test_confusion_route_requires_api_key(monkeypatch):
    response = client.get("/evaluate/confusion")
    assert response.status_code == 401, response.text
