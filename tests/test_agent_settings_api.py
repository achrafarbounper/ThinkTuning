"""
Tests offline des paramètres persistants de l'agent IA (/api/agent/settings).

Couvre :
    - core.agent_settings : lecture/écriture SQLite, validation ;
    - core.agent_cache.agent_config : priorité sqlite > env > défaut,
      température propagée dans la fabrique de runners ;
    - GET /api/agent/settings : valeurs effectives + clé masquée ;
    - PUT /api/agent/settings : persistance + rechargement immédiat du
      runner (effet sur list_llm_models sans redémarrage), erreurs 400 ;
    - POST /api/agent/settings/test : sonde Ollama et OpenRouter mockée.

Aucun appel réseau ni accès à experiments/agent_settings.db : chaque test
repart d'une base isolée dans tmp_path.
Lance avec : pytest tests/test_agent_settings_api.py -v
"""

import json
import os
import sqlite3

# Config test AVANT tout import.
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("AGENT_OLLAMA_URL", "http://127.0.0.1:9/api/chat")  # port factice

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api import app  # noqa: E402
from core import agent_cache  # noqa: E402
from core import agent_settings as agent_settings_module  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}
client = TestClient(app)

DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Base SQLite vierge par test + purge des variables AGENT_*/OPENROUTER_*."""
    db_path = str(tmp_path / "agent_settings.db")
    agent_settings_module.reset_store_for_tests(db_path)
    for var in (
        "AGENT_PROVIDER",
        "AGENT_MODEL_NAME",
        "AGENT_OPENROUTER_URL",
        "AGENT_TIMEOUT_SECONDS",
        "AGENT_CONTEXT_LENGTH",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# --- core.agent_settings : store brut --------------------------------------------------


def test_store_save_and_read_roundtrip(tmp_path):
    store = agent_settings_module.AgentSettingsStore(str(tmp_path / "s.db"))
    written = store.save_many({"provider": "openrouter", "model": "a/b"})
    assert sorted(written) == ["model", "provider"]
    assert store.get_all() == {"provider": "openrouter", "model": "a/b"}

    # Les clés inconnues sont ignorées silencieusement ; l'upsert écrase.
    assert store.save_many({"not_a_key": 1}) == {}
    store.save_many({"model": "c/d"})
    assert store.get_all()["model"] == "c/d"


def test_store_writes_visible_in_sqlite_file(tmp_path):
    """La persistance est réellement sur disque (nouvelle instance relit tout)."""
    db_path = str(tmp_path / "s.db")
    agent_settings_module.AgentSettingsStore(db_path).save_many(
        {"context_length": 8192}
    )

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT key, value FROM agent_settings").fetchall()
    conn.close()
    parsed = {key: json.loads(value) for key, value in rows}
    assert parsed["context_length"] == 8192


def test_validate_rejects_bad_values():
    errors = agent_settings_module.validate_agent_settings(
        {"provider": "mistral", "timeout_seconds": 5, "temperature": 9}
    )
    joined = " ".join(errors)
    assert "provider" in joined
    assert "timeout_seconds" in joined
    assert "temperature" in joined

    # Cas valides : aucune erreur, coercitions appliquées.
    values = {"timeout_seconds": "120", "context_length": "4096"}
    assert agent_settings_module.validate_agent_settings(values) == []
    assert values == {"timeout_seconds": 120.0, "context_length": 4096}


def test_validate_refuses_empty_key_with_openrouter():
    errors = agent_settings_module.validate_agent_settings(
        {"provider": "openrouter", "openrouter_api_key": "   "}
    )
    assert any("openrouter_api_key" in err for err in errors)


# --- core.agent_cache : priorité sqlite > env > défaut ---------------------------------


def test_agent_config_env_fallback_when_db_empty(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "OpenRouter")
    cfg = agent_cache.agent_config()
    assert cfg["provider"] == "openrouter"
    assert cfg["model"] == agent_cache.DEFAULT_OPENROUTER_MODEL_NAME
    assert cfg["ollama_url"] == os.environ["AGENT_OLLAMA_URL"]


def test_agent_config_sqlite_overrides_env(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "ollama")
    agent_settings_module.save_many(
        {"provider": "openrouter", "model": "vendor/model-x"}
    )
    cfg = agent_cache.agent_config()
    assert cfg["provider"] == "openrouter"
    assert cfg["model"] == "vendor/model-x"


def test_build_runner_forwards_temperature_from_settings():
    captured = {}

    class Probe:
        def __init__(self, url, model, **kwargs):
            captured.update(kwargs)
            captured["url"] = url
            captured["model"] = model

    agent_settings_module.save_many({"temperature": 0.3, "provider": "ollama"})
    original = agent_cache.LLMClient
    agent_cache.LLMClient = Probe
    try:
        agent_cache._build_runner()
    finally:
        agent_cache.LLMClient = original

    assert captured["provider"] == "ollama"
    assert captured["temperature"] == pytest.approx(0.3)


def test_llm_endpoint_accepts_key_saved_in_db():
    agent_settings_module.save_many(
        {"provider": "openrouter", "openrouter_api_key": "sk-or-db-key"}
    )
    url, key = agent_cache._llm_endpoint(agent_cache.agent_config())
    assert url == DEFAULT_OPENROUTER_URL
    assert key == "sk-or-db-key"


# --- GET /api/agent/settings -----------------------------------------------------------


def test_get_settings_requires_auth():
    assert client.get("/api/agent/settings").status_code == 401


def test_get_settings_returns_effective_values_without_clear_key():
    os.environ["OPENROUTER_API_KEY"] = "sk-or-v1-secretsecretabcd"
    try:
        resp = client.get("/api/agent/settings", headers=HEADERS)
    finally:
        os.environ.pop("OPENROUTER_API_KEY", None)

    assert resp.status_code == 200
    body = resp.json()
    assert body["settings"]["provider"] == "ollama"  # défaut (DB vide)
    assert body["settings"]["has_openrouter_api_key"] is True
    # La clé n'apparaît jamais en clair : uniquement masquée.
    assert "openrouter_api_key" not in body["settings"]
    assert body["settings"]["openrouter_api_key_masked"].endswith("abcd")
    assert "secretsecret" not in resp.text
    # Sources exposées pour l'affichage « d'où vient cette valeur ».
    assert body["sources"]["provider"] == "default"
    assert body["sources"]["openrouter_api_key"] == "env"


# --- PUT /api/agent/settings ------------------------------------------------------------


def test_put_settings_persists_and_reloads_runner(monkeypatch):
    """PUT persiste en SQLite et recharge le runner immédiatement."""
    import api.routes.agent as agent_routes

    reloaded = []
    monkeypatch.setattr(
        agent_routes, "reload_agent_runner", lambda: reloaded.append(True) or "runner"
    )

    resp = client.put(
        "/api/agent/settings",
        headers=HEADERS,
        json={
            "provider": "openrouter",
            "model": "vendor/saved-model",
            "timeout_seconds": 120,
            "context_length": 8192,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["settings"]["provider"] == "openrouter"
    assert body["settings"]["model"] == "vendor/saved-model"
    assert body["settings"]["timeout_seconds"] == 120
    assert sorted(body["written_keys"]) == [
        "context_length", "model", "provider", "timeout_seconds"
    ]
    assert reloaded  # reload_agent_runner a bien été appelé

    # La valeur est en base ET visible par agent_config sans variable d'env.
    stored = agent_cache.get_agent_settings()
    assert stored["model"]["source"] == "sqlite"
    cfg = agent_cache.agent_config()
    assert cfg["model"] == "vendor/saved-model"
    assert cfg["timeout"] == pytest.approx(120.0)
    assert cfg["context_length"] == 8192


def test_put_settings_invalid_provider_returns_400(monkeypatch):
    import api.routes.agent as agent_routes

    reloaded = []
    monkeypatch.setattr(agent_routes, "reload_agent_runner", lambda: reloaded.append(1))
    resp = client.put("/api/agent/settings", headers=HEADERS, json={"provider": "gpt4"})
    assert resp.status_code in (400, 422)
    assert not reloaded  # rien n'a été rechargé


def test_put_settings_empty_key_with_openrouter_rejected():
    resp = client.put(
        "/api/agent/settings",
        headers=HEADERS,
        json={"provider": "openrouter", "openrouter_api_key": ""},
    )
    assert resp.status_code == 400


def test_put_partial_update_keeps_other_keys():
    client.put("/api/agent/settings", headers=HEADERS, json={"model": "first:8b"})
    resp = client.put("/api/agent/settings", headers=HEADERS, json={"timeout_seconds": 30})
    assert resp.status_code == 200
    body = agent_cache.get_agent_settings()
    assert body["model"]["value"] == "first:8b"  # inchangé
    assert body["timeout_seconds"]["value"] == 30.0


# --- POST /api/agent/settings/test ------------------------------------------------------


class FakeProbeResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"models": [], "data": []}


def test_test_endpoint_probes_ollama_with_draft_values(monkeypatch):
    """Le bouton Tester peut sonder des valeurs NON encore sauvegardées."""
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured.update({"url": url, "headers": headers})
        return FakeProbeResponse()

    monkeypatch.setattr("api.routes.agent.requests.get", fake_get)

    resp = client.post(
        "/api/agent/settings/test",
        headers=HEADERS,
        json={"provider": "ollama", "ollama_url": "http://host.lan:11434/api/chat"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert captured["url"] == "http://host.lan:11434/api/tags"


def test_test_endpoint_openrouter_sends_bearer(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        assert "/models" in url
        assert headers["Authorization"] == "Bearer sk-or-draft"
        return FakeProbeResponse()

    monkeypatch.setattr("api.routes.agent.requests.get", fake_get)

    resp = client.post(
        "/api/agent/settings/test",
        headers=HEADERS,
        json={
            "provider": "openrouter",
            "openrouter_url": DEFAULT_OPENROUTER_URL,
            "openrouter_api_key": "sk-or-draft",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_test_endpoint_reports_unreachable_provider(monkeypatch):
    import requests as requests_lib

    def fake_get(url, headers=None, timeout=None):
        raise requests_lib.exceptions.ConnectionError("refusé")

    monkeypatch.setattr("api.routes.agent.requests.get", fake_get)

    resp = client.post(
        "/api/agent/settings/test", headers=HEADERS, json={"provider": "ollama"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "Injoignable" in body["detail"]
