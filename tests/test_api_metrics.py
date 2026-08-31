import os

os.environ.setdefault("API_KEY", "test-key")

from fastapi.testclient import TestClient

import api
from api import app


client = TestClient(app)


def test_metrics_endpoint_exposes_prometheus_data():
    response = client.get("/metrics")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body


def test_metrics_json_endpoint_returns_structured_snapshot():
    # Génère au moins une observation pour que l'histogramme soit peuplé.
    client.get("/health", headers={"X-API-Key": "test-key"})

    response = client.get("/metrics/json")

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/json")
    payload = response.json()

    assert "scrape_at_ms" in payload
    assert isinstance(payload["scrape_at_ms"], int)

    counters = payload["counters"]
    histograms = payload["histograms"]

    assert isinstance(counters, list) and isinstance(histograms, list)

    counter_names = [c["name"] for c in counters]
    assert "http_requests_total" in counter_names

    hist_by_name = {h["name"]: h for h in histograms}
    assert "http_request_duration_seconds" in hist_by_name
    hist = hist_by_name["http_request_duration_seconds"]
    assert hist["count"] >= 1  # une observation /health a été enregistrée
    assert hist["sum"] >= 0

    # Cohérence : total requêtes du compteur >= nombre d'entrées par path/statut.
    total = sum(c["value"] for c in counters if c["name"] == "http_requests_total")
    assert total >= 1


def test_structured_logs_include_event_metadata(caplog):
    with caplog.at_level("INFO", logger="thinktuning.api"):
        response = client.get("/health", headers={"X-API-Key": "test-key"})

    assert response.status_code == 200, response.text
    assert any("http_request" in record.getMessage() for record in caplog.records)
