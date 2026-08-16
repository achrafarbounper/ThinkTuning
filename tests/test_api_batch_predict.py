import csv
import io

from fastapi.testclient import TestClient

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
