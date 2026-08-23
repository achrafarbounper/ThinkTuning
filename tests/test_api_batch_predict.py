import csv
import io
import os

os.environ.setdefault("API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from api import app


client = TestClient(app)


def test_predict_batch_json_route():
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["text"])
    writer.writerow(["J'adore ce produit !"])
    writer.writerow(["Je suis très déçu par cet achat."])

    response = client.post(
        "/predict/batch",
        files={"file": ("batch.csv", csv_buffer.getvalue(), "text/csv")},
        data={"text_column": "text", "response_format": "json"},
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert "results" in payload
    assert len(payload["results"]) == 2
    assert all("text" in item and "sentiment" in item and "confidence" in item for item in payload["results"])


def test_predict_batch_csv_route():
    csv_buffer = io.StringIO()
    writer = csv.writer(csv_buffer)
    writer.writerow(["text"])
    writer.writerow(["C'est un bon service."])

    response = client.post(
        "/predict/batch",
        files={"file": ("batch.csv", csv_buffer.getvalue(), "text/csv")},
        data={"text_column": "text", "response_format": "csv"},
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    body = response.text.strip().splitlines()
    assert body[0].startswith("row_index,text,sentiment,confidence")
    assert len(body) == 2


def test_predict_route_rate_limit(monkeypatch):
    class FakePredictor:
        def predict(self, texts):
            return [
                {"text": text, "sentiment": "positive", "confidence": 0.95}
                for text in texts
            ]

    monkeypatch.setattr(api, "RATE_LIMIT_PER_MINUTE", 1)
    api._reset_rate_limit_buckets()
    monkeypatch.setattr(api, "_get_predictor", lambda model=None: FakePredictor())

    first = client.post(
        "/predict",
        json={"texts": ["C'est un très bon service."]},
        headers={"X-API-Key": "test-key"},
    )
    second = client.post(
        "/predict",
        json={"texts": ["Ce produit est médiocre."]},
        headers={"X-API-Key": "test-key"},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 429, second.text
    assert "Retry-After" in second.headers
    assert second.headers["Retry-After"].isdigit()


def test_predict_batch_route_rate_limit(monkeypatch):
    class FakePredictor:
        def predict(self, texts):
            return [{"text": text, "sentiment": "positive", "confidence": 0.95} for text in texts]

    monkeypatch.setattr(api, "RATE_LIMIT_PER_MINUTE", 1)
    api._reset_rate_limit_buckets()
    monkeypatch.setattr(api, "_get_predictor", lambda model=None: FakePredictor())

    csv_payload = "text\nC'est un très bon service.\n"
    first = client.post(
        "/predict/batch",
        files={"file": ("batch.csv", csv_payload, "text/csv")},
        data={"text_column": "text", "response_format": "json"},
        headers={"X-API-Key": "test-key"},
    )
    second = client.post(
        "/predict/batch",
        files={"file": ("batch.csv", csv_payload, "text/csv")},
        data={"text_column": "text", "response_format": "json"},
        headers={"X-API-Key": "test-key"},
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 429, second.text
    assert "Retry-After" in second.headers
    assert second.headers["Retry-After"].isdigit()


def test_list_models_route(monkeypatch, tmp_path):
    # Crée un dossier de modèle valide
    version_dir = tmp_path / "20240102T120000Z"
    version_dir.mkdir()
    (version_dir / "training_report.json").write_text("{}")  # rapport minimal

    # Crée un dossier de modèle valide plus ancien
    older_dir = tmp_path / "20240101T120000Z"
    older_dir.mkdir()
    (older_dir / "training_report.json").write_text("{}")

    monkeypatch.setattr(api, "MODEL_ROOT", str(tmp_path))
    monkeypatch.setattr(api, "MODELS_ROOT", str(tmp_path))

    response = client.get("/models", headers={"X-API-Key": "test-key"})

    assert response.status_code == 200, response.text
    payload = response.json()

    assert [item["name"] for item in payload] == [
        "20240102T120000Z",
        "20240101T120000Z",
    ]
    assert payload[0]["path"].endswith("20240102T120000Z")


def test_models_route_requires_api_key(monkeypatch):
    monkeypatch.setattr(api, "API_KEY", "test-key")

    response = client.get("/models")
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "Invalid or missing X-API-Key header."

    response = client.get("/models", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200, response.text


def test_compare_route(monkeypatch):
    class FakePredictor:
        def predict(self, texts):
            return [
                {"text": texts[0], "sentiment": "positive", "confidence": 0.91},
                {"text": texts[1], "sentiment": "negative", "confidence": 0.68},
            ]

    monkeypatch.setattr(api, "_get_predictor", lambda model=None: FakePredictor())

    response = client.post(
        "/compare",
        json={"text_a": "J'aime beaucoup ce produit.", "text_b": "Je déteste ce produit."},
        headers={"X-API-Key": "test-key"},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["text_a"]["sentiment"] == "positive"
    assert payload["text_b"]["sentiment"] == "negative"
    assert payload["confidence_diff"] == 0.23
    assert payload["sentiments_identical"] is False
    assert payload["sentiments_opposed"] is True
    assert payload["comparison"] == "opposed"


def test_batch_json():
    response = client.post(
        "/predict/batch",
        files={"file": ("test.csv", "text\nhello\nworld")},
        data={"response_format": "json"},
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 200
    data = response.json()
    assert "results" in data


def test_batch_csv():
    response = client.post(
        "/predict/batch",
        files={"file": ("test.csv", "text\nhello\nworld")},
        data={"response_format": "csv"},
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 200
    content_disposition = response.headers.get("Content-Disposition")
    assert content_disposition is not None


def test_batch_parquet():
    response = client.post(
        "/predict/batch",
        files={"file": ("test.csv", "text\nhello\nworld")},
        data={"response_format": "parquet"},
        headers={"X-API-Key": "test-key"},
    )

    # 1. Statut OK
    assert response.status_code == 200

    # 2. En-tête présent
    content_disposition = response.headers.get("Content-Disposition")
    assert content_disposition is not None
    assert "predictions.parquet" in content_disposition

    # 3. Le contenu doit être lisible en parquet
    import pandas as pd
    import io

    raw = response.content
    df = pd.read_parquet(io.BytesIO(raw))

    # 4. Vérifier les colonnes attendues
    expected_columns = {"row_index", "text", "sentiment", "confidence"}
    assert expected_columns.issubset(df.columns)

    # 5. Vérifier les valeurs
    assert df.loc[0, "text"] == "hello"
    assert df.loc[1, "text"] == "world"

    # Les sentiments sont mockés, donc on vérifie juste qu'ils existent
    assert df.loc[0, "sentiment"] in {"positive", "negative", "neutral"}
    assert isinstance(df.loc[0, "confidence"], float)


def test_batch_invalid_format():
    response = client.post(
        "/predict/batch",
        files={"file": ("test.csv", "text\nhello"),},
        data={"response_format": "invalid"},
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 400


def test_batch_missing_column():
    response = client.post(
        "/predict/batch",
        files={"file": ("test.csv", "other\nhello"),},
        data={"text_column": "nonexistent", "response_format": "json"},
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 400


def test_batch_empty_file():
    response = client.post(
        "/predict/batch",
        files={"file": ("test.csv", "")},
        data={"response_format": "json"},
        headers={"X-API-Key": "test-key"},
    )
    assert response.status_code == 400