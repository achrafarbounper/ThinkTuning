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


def test_structured_logs_include_event_metadata(caplog):
    with caplog.at_level("INFO", logger="thinktuning.api"):
        response = client.get("/health", headers={"X-API-Key": "test-key"})

    assert response.status_code == 200, response.text
    assert any("http_request" in record.getMessage() for record in caplog.records)
