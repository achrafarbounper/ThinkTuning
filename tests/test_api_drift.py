# project/tests/test_api_drift.py
"""
Tests offline de l'endpoint POST /drift (détection de dérive entre batches).

Prédicteur factice monkeypatché sur api._get_predictor : chaque texte est
mappé vers un label fixe, ce qui permet de contrôler exactement les
distributions des deux batches.
"""

import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("API_KEY", "test-key")

from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402


class FakePredictor:
    """Predictor déterministe : PRED_MAP[texte] -> label, défaut 'positive'."""

    def __init__(self, pred_map=None, default="positive"):
        self.pred_map = pred_map or {}
        self.default = default

    def predict(self, texts):
        return [
            {"text": t, "sentiment": self.pred_map.get(t, self.default), "confidence": 0.9}
            for t in texts
        ]


@pytest.fixture()
def client():
    return TestClient(api.app)


HEADERS = {"X-API-Key": "test-key"}


def _setup(monkeypatch, pred_map=None, default="positive"):
    monkeypatch.setattr(api, "_get_predictor", lambda model=None: FakePredictor(pred_map, default))


def test_drift_identical_distributions_no_alert(client, monkeypatch):
    _setup(monkeypatch)
    texts_a = ["a", "b", "c", "d"]
    texts_b = ["a", "b", "c", "d"]
    resp = client.post("/drift", json={"texts_a": texts_a, "texts_b": texts_b}, headers=HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["method"] == "kl"
    assert data["drift_score"] == pytest.approx(0.0, abs=1e-5)
    assert data["drift_detected"] is False
    assert data["p_value"] is None


def test_drift_different_distributions_alert(client, monkeypatch):
    # A : moitié positive / moitié negative ; B : tout positive -> KL élevée.
    _setup(monkeypatch, pred_map={"neg1": "negative", "neg2": "negative"})
    resp = client.post(
        "/drift",
        json={"texts_a": ["neg1", "neg2", "p1", "p2"], "texts_b": ["p1", "p2", "p3", "p4"]},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["drift_score"] > 0.1
    assert data["drift_detected"] is True


def test_drift_threshold_configurable(client, monkeypatch):
    # Score > 0 mais modéré : le flag change selon le seuil.
    _setup(monkeypatch, pred_map={"neg1": "negative"})
    body = {"texts_a": ["neg1", "p1", "p2", "p3"], "texts_b": ["p1", "p2", "p3", "p4"]}
    low = client.post("/drift", json={**body, "threshold": 0.01}, headers=HEADERS).json()
    high = client.post("/drift", json={**body, "threshold": 10.0}, headers=HEADERS).json()
    assert low["drift_detected"] is True
    assert high["drift_detected"] is False
    assert low["threshold"] == 0.01 and high["threshold"] == 10.0


def test_drift_chi2_method(client, monkeypatch):
    _setup(monkeypatch, pred_map={"neg1": "negative", "neg2": "negative", "neu1": "neutral"})
    resp = client.post(
        "/drift",
        params={"method": "chi2", "threshold": 0.05},
        json={"texts_a": ["neg1", "neg2", "neu1", "p1"], "texts_b": ["p1", "p2", "p3", "p4"]},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["method"] == "chi2"
    assert data["p_value"] is not None
    assert data["drift_detected"] is (data["p_value"] < 0.05)
    # Distributions très différentes -> dérive détectée.
    assert data["drift_detected"] is True

def test_drift_invalid_method(client, monkeypatch):
    _setup(monkeypatch)
    resp = client.post(
        "/drift", params={"method": "js"}, json={"texts_a": ["a"], "texts_b": ["b"]}, headers=HEADERS
    )
    assert resp.status_code == 400


def test_drift_invalid_threshold(client, monkeypatch):
    _setup(monkeypatch)
    resp = client.post(
        "/drift", params={"threshold": 0}, json={"texts_a": ["a"], "texts_b": ["b"]}, headers=HEADERS
    )
    assert resp.status_code == 400


def test_drift_empty_batch(client, monkeypatch):
    _setup(monkeypatch)
    resp = client.post("/drift", json={"texts_a": [], "texts_b": ["a"]}, headers=HEADERS)
    assert resp.status_code == 400


def test_drift_missing_fields(client, monkeypatch):
    _setup(monkeypatch)
    resp = client.post("/drift", json={"texts_a": ["a"]}, headers=HEADERS)
    assert resp.status_code == 400


def test_drift_csv_upload(client, monkeypatch):
    _setup(monkeypatch, pred_map={"neg1": "negative", "neg2": "negative"})
    csv_a = io.BytesIO(b"text\nneg1\nneg2\np1\np2\n")
    csv_b = io.BytesIO(b"text\np1\np2\np3\np4\n")
    resp = client.post(
        "/drift",
        files={"file_a": ("a.csv", csv_a, "text/csv"), "file_b": ("b.csv", csv_b, "text/csv")},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["n_a"] == 4 and data["n_b"] == 4
    assert data["drift_detected"] is True
    assert data["distribution_a"]["positive"] == 0.5
    assert data["distribution_b"]["positive"] == 1.0


def test_drift_csv_custom_column(client, monkeypatch):
    _setup(monkeypatch)
    csv_a = io.BytesIO(b"review\nx\ny\n")
    csv_b = io.BytesIO(b"review\nx\ny\n")
    resp = client.post(
        "/drift",
        params={"text_column": "review"},
        files={"file_a": ("a.csv", csv_a, "text/csv"), "file_b": ("b.csv", csv_b, "text/csv")},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    assert resp.json()["drift_score"] == pytest.approx(0.0, abs=1e-5)


def test_drift_csv_missing_column(client, monkeypatch):
    _setup(monkeypatch)
    csv_a = io.BytesIO(b"body\nx\n")
    csv_b = io.BytesIO(b"text\ny\n")
    resp = client.post(
        "/drift",
        files={"file_a": ("a.csv", csv_a, "text/csv"), "file_b": ("b.csv", csv_b, "text/csv")},
        headers=HEADERS,
    )
    assert resp.status_code == 400


def test_drift_csv_invalid(client, monkeypatch):
    _setup(monkeypatch)
    bad = io.BytesIO(b"\x00\xff not csv")
    ok = io.BytesIO(b"text\ny\n")
    resp = client.post(
        "/drift",
        files={"file_a": ("a.csv", bad, "text/csv"), "file_b": ("b.csv", ok, "text/csv")},
        headers=HEADERS,
    )
    assert resp.status_code == 400


def test_drift_requires_api_key(client, monkeypatch):
    _setup(monkeypatch)
    resp = client.post("/drift", json={"texts_a": ["a"], "texts_b": ["b"]})
    assert resp.status_code in (401, 403)

def test_drift_chi2_zero_expected_label(client, monkeypatch):
    # Batch A sans label "neutral" : l'effectif attendu pour ce label vaut 0,
    # ce qui produisait un NaN (division 0/0) et un 500. Doit réussir désormais.
    _setup(monkeypatch, pred_map={"neu1": "neutral", "neg1": "negative", "neg2": "negative"})
    resp = client.post(
        "/drift",
        params={"method": "chi2", "threshold": 0.05},
        json={"texts_a": ["neg1", "neg2", "p1", "p2"], "texts_b": ["neu1", "p1", "p2", "p3"]},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["p_value"] is not None
    assert data["drift_detected"] is (data["p_value"] < 0.05)


def test_drift_chi2_single_label_reference(client, monkeypatch):
    # Batch A à 100 % sur un seul label : le khi-deux n'est pas calculable.
    _setup(monkeypatch)
    resp = client.post(
        "/drift",
        params={"method": "chi2"},
        json={"texts_a": ["p1", "p2", "p3"], "texts_b": ["p4", "p5"]},
        headers=HEADERS,
    )
    assert resp.status_code == 400
    assert "khi-deux" in resp.json()["detail"]


def test_drift_chi2_no_nan_in_response(client, monkeypatch):
    # Garde-fou : aucune réponse ne doit contenir de NaN (sérialisation JSON).
    _setup(monkeypatch, pred_map={"neu1": "neutral", "neg1": "negative"})
    resp = client.post(
        "/drift",
        params={"method": "chi2"},
        json={
            "texts_a": ["neu1", "neg1", "p1", "p2", "p3", "p4"],
            "texts_b": ["neu1", "neu1", "p5", "p6"],
        },
        headers=HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["drift_score"] == data["drift_score"]  # NaN != NaN sinon
    assert data["p_value"] == data["p_value"]
