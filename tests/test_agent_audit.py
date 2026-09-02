# project/tests/test_agent_audit.py

"""Tests offline du journal d'audit (Phase A, compliance) : `core/audit_store.py`,
du module de feature flags (`core/feature_flags.py`) et de l'endpoint
`GET /api/agent/audit`. Aucun réseau : base SQLite isolée dans tmp_path.

Lance avec : pytest tests/test_agent_audit.py -v
"""

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402,F401
from api import app  # noqa: E402
from core import (
    audit_store,  # noqa: E402
    feature_flags,  # noqa: E402
)

HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture()
def store(tmp_path):
    """AuditStore sur une base neuve par test."""
    return audit_store.AuditStore(str(tmp_path / "audit.db"))


@pytest.fixture()
def client(tmp_path):
    """TestClient avec un journal d'audit isolé par test."""
    from core import audit_store as _as

    _as.reset_audit_store(str(tmp_path / "api_audit.db"))
    return TestClient(app)


# --- Feature flags ------------------------------------------------------------------


def test_feature_flags_off_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_RELIABILITY", raising=False)
    monkeypatch.delenv("AGENT_AUDIT", raising=False)
    assert feature_flags.flag("reliability") is False
    assert feature_flags.flag("audit") is False
    assert feature_flags.active_features() == []


def test_feature_flag_toggle(monkeypatch):
    monkeypatch.setenv("AGENT_RELIABILITY", "1")
    monkeypatch.setenv("AGENT_AUDIT", "on")
    assert feature_flags.flag("reliability") is True
    assert feature_flags.flag("audit") is True
    assert "reliability" in feature_flags.active_features()
    # Valeurs non « activantes » : désactivé.
    for v in ("0", "off", "false", "", "no"):
        monkeypatch.setenv("AGENT_RELIABILITY", v)
        assert feature_flags.flag("reliability") is False


def test_unknown_flag_false(monkeypatch):
    monkeypatch.setenv("AGENT_DOES_NOT_EXIST", "1")
    assert feature_flags.flag("does_not_exist") is False


# --- AuditStore ----------------------------------------------------------------------


def test_audit_log_query_roundtrip(store):
    row = store.log(
        audit_store.ACT_RUN,
        subject="ask",
        detail={"status": "started", "model": "m:1"},
        actor="tester",
        ip="127.0.0.1",
        run_id="run-1",
    )
    assert row["id"]
    assert row["action"] == audit_store.ACT_RUN
    assert row["actor"] == "tester"
    assert row["run_id"] == "run-1"
    assert row["detail"] == {"status": "started", "model": "m:1"}
    assert row["ts"].endswith("Z") or "T" in row["ts"]

    # Relecture par id et par requête filtrée.
    assert store.get(row["id"])["action"] == audit_store.ACT_RUN
    res = store.query(action=audit_store.ACT_RUN, run_id="run-1")
    assert res["total"] == 1
    assert res["items"][0]["id"] == row["id"]
    assert res["limit"] == 100


def test_audit_redacts_sensitive_keys(store):
    detail = {
        "tool": "http_post",
        "args": {"url": "https://x", "api_key": "sk-secret-that-must-not-leak",
                 "headers": {"Authorization": "Bearer tok123"}},
        "params": {"password": "hunter2", "safe": 42},
    }
    row = store.log(audit_store.ACT_TOOL, subject="http_post", detail=detail)
    stored = row["detail"]
    # Clés sensibles anonymisées récursivement.
    assert stored["args"]["api_key"] == "[REDACTED]"
    assert stored["args"]["headers"]["Authorization"] == "[REDACTED]"
    assert stored["params"]["password"] == "[REDACTED]"
    # Champ non sensible conservé.
    assert stored["params"]["safe"] == 42


def test_audit_truncates_long_values(store):
    row = store.log(
        audit_store.ACT_TOOL, subject="read_file",
        detail={"content": "x" * audit_store._MAX_STRING_CHARS * 2},
    )
    assert len(row["detail"]["content"]) <= audit_store._MAX_STRING_CHARS
    assert row["detail"]["content"].endswith("…[tronqué]")


def test_audit_get_unknown_none(store):
    assert store.get("absent") is None


def test_audit_query_pagination(store):
    for i in range(5):
        store.log(audit_store.ACT_RUN, subject="ask", detail={"i": i})
    res = store.query(limit=2, offset=0)
    assert res["total"] == 5
    assert len(res["items"]) == 2
    res2 = store.query(limit=2, offset=2)
    assert len(res2["items"]) == 2


def test_audit_singleton_reset(tmp_path):
    audit_store.reset_audit_store(str(tmp_path / "shared_audit.db"))
    stored = audit_store.get_audit_store()
    stored.log(audit_store.ACT_RUN, subject="x")
    assert audit_store.get_audit_store().query()["total"] == 1


# --- Endpoint ------------------------------------------------------------------------


def test_audit_endpoint_requires_flag(client):
    # Flag off par défaut -> 403 explicite.
    resp = client.get("/api/agent/audit", headers=HEADERS)
    assert resp.status_code == 403
    assert "audit" in resp.json()["detail"]


def test_features_endpoint(client):
    resp = client.get("/api/agent/features", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert {"reliability", "audit", "websocket"} <= set(body["features"])
    assert "active" in body
