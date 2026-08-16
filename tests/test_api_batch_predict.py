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
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/csv")
    body = response.text.strip().splitlines()
    assert body[0].startswith("row_index,text,sentiment,confidence")
    assert len(body) == 2


def test_list_models_route(monkeypatch, tmp_path):
    version_dir = tmp_path / "20240102T120000Z"
    version_dir.mkdir()
    older_dir = tmp_path / "20240101T120000Z"
    older_dir.mkdir()

    monkeypatch.setattr(api, "MODEL_ROOT", str(tmp_path))
    monkeypatch.setattr(api, "MODELS_ROOT", str(tmp_path))

    response = client.get("/models")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert [item["name"] for item in payload] == ["20240102T120000Z", "20240101T120000Z"]
    assert payload[0]["path"].endswith("20240102T120000Z")


def test_models_route_requires_api_key(monkeypatch):
    monkeypatch.setattr(api, "API_KEY", "test-key")

    response = client.get("/models")
    assert response.status_code == 401, response.text
    assert response.json()["detail"] == "Invalid or missing X-API-Key header."

    response = client.get("/models", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200, response.text
