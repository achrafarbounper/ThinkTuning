"""
Validation des entrées des endpoints de prédiction (POST /predict, /predict/batch).

Objectifs :
    - textes vides / trop nombreux -> 422 (Pydantic), jamais 500 tokenizer ;
    - batch : taille de fichier (413), nombre de lignes (400), cellules vides
      (400), format invalide (400) — sans jamais charger de vrai modèle.
"""
import csv
import io
import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from fastapi.testclient import TestClient

import api
from api import app
from api.routes import predict as predict_module

client = TestClient(app)
HEADERS = {"X-API-Key": "test-key"}


class FakePredictor:
    """Prédicteur offline : renvoie le texte en « positive » avec 0.9."""

    def predict(self, texts):
        return [{"text": t, "sentiment": "positive", "confidence": 0.9} for t in texts]


@pytest.fixture(autouse=True)
def fake_predictor(monkeypatch):
    monkeypatch.setattr(api, "_get_predictor", lambda model=None: FakePredictor())


def _csv_bytes(rows):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["text"])
    for row in rows:
        writer.writerow([row])
    return buf.getvalue()


# ---------------------------------------------------------------------------
# POST /predict — validation du schéma Pydantic
# ---------------------------------------------------------------------------

def test_predict_empty_texts_rejected():
    response = client.post("/predict", json={"texts": []}, headers=HEADERS)
    assert response.status_code == 422, response.text


def test_predict_valid_payload():
    response = client.post(
        "/predict", json={"texts": ["Super film", "Nul"]}, headers=HEADERS
    )
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert [r["sentiment"] for r in results] == ["positive", "positive"]


def test_predict_too_many_texts_rejected():
    # 300 textes > borne par défaut (256). La borne est figée dans le modèle
    # Pydantic à l'import : on teste la borne réelle, pas un monkeypatch.
    texts = [f"texte numéro {i}" for i in range(300)]
    response = client.post("/predict", json={"texts": texts}, headers=HEADERS)
    assert response.status_code == 422, response.text


def test_predict_text_too_long_rejected():
    response = client.post(
        "/predict", json={"texts": ["x" * 10001]}, headers=HEADERS
    )
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# POST /predict/batch — garde-fous fichier / lignes / cellules
# ---------------------------------------------------------------------------

def test_batch_nan_cells_rejected():
    # Une cellule vide ("" en CSV) est lue comme NaN par pandas : le routeur
    # doit la refuser explicitement (le tokenizer planterait sinon).
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["text"])
    writer.writerow(["Bonjour"])
    writer.writerow([""])
    response = client.post(
        "/predict/batch",
        files={"file": ("batch.csv", buf.getvalue(), "text/csv")},
        data={"text_column": "text", "response_format": "json"},
        headers=HEADERS,
    )
    assert response.status_code == 400, response.text
    assert "vides" in response.json()["detail"]


def test_batch_too_many_rows_rejected(monkeypatch):
    monkeypatch.setattr(predict_module, "MAX_BATCH_ROWS", 2)
    response = client.post(
        "/predict/batch",
        files={"file": ("batch.csv", _csv_bytes(["a", "b", "c"]), "text/csv")},
        data={"text_column": "text", "response_format": "json"},
        headers=HEADERS,
    )
    assert response.status_code == 400, response.text
    assert "lignes" in response.json()["detail"]


def test_batch_oversize_file_rejected(monkeypatch):
    # Le CSV "text\r\na\r\nb\r\n" fait ~12 octets : on abaisse la borne à 8
    # (constante lue dans le corps de la route, donc monkeypatchable).
    monkeypatch.setattr(predict_module, "MAX_UPLOAD_BYTES", 8)
    response = client.post(
        "/predict/batch",
        files={"file": ("batch.csv", _csv_bytes(["a", "b"]), "text/csv")},
        data={"text_column": "text", "response_format": "json"},
        headers=HEADERS,
    )
    assert response.status_code == 413, response.text


def test_batch_chunks_preserve_order(monkeypatch):
    """L'inférence par chunks conserve l'ordre et agrège tous les résultats."""
    monkeypatch.setattr(predict_module, "PREDICT_CHUNK_SIZE", 1)
    response = client.post(
        "/predict/batch",
        files={"file": ("batch.csv", _csv_bytes(["un", "deux", "trois"]), "text/csv")},
        data={"text_column": "text", "response_format": "json"},
        headers=HEADERS,
    )
    assert response.status_code == 200, response.text
    rows = response.json()["results"]
    assert [r["text"] for r in rows] == ["un", "deux", "trois"]
    assert [r["row_index"] for r in rows] == [0, 1, 2]
