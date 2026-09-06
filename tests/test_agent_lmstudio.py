"""
Tests offline de l'intégration du provider LM Studio (« lm_studio ») dans
l'agent IA.

Miroir de tests/test_agent_hf.py : payload compatible OpenAI, AUCUNE
authentification (serveur local), sélection du provider, fabrique de runners,
liste des modèles GET /v1/models, sonde POST /api/agent/settings/test et noyau
v2 (factory + HttpLLMClient). Aucun appel réseau.
Lance avec : pytest tests/test_agent_lmstudio.py -v
"""

import json
import os
import tempfile

os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("AGENT_OLLAMA_URL", "http://127.0.0.1:9/api/chat")

_SETTINGS_DB = os.path.join(
    tempfile.mkdtemp(prefix="tt-settings-lmstudio-"), "agent_settings.db"
)
os.environ.setdefault("AGENT_SETTINGS_PATH", _SETTINGS_DB)

import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api import app  # noqa: E402
from core import agent_cache  # noqa: E402
from core import agent_settings as agent_settings_module  # noqa: E402

agent_settings_module.reset_store_for_tests(_SETTINGS_DB)

from ia.agent import llm_client as llm_module  # noqa: E402
from ia.agent.llm_client import LLMClient  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}
client = TestClient(app)

DEFAULT_LM_STUDIO_URL = "http://192.168.184:1234/v1/chat/completions"
DEFAULT_LM_STUDIO_ROOT = "http://192.168.184:1234/v1"


class FakeSSEResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        pass

    def iter_lines(self, decode_unicode=True):
        return iter(self._lines)

    def close(self):
        pass


class FakeJSONResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class FakeProbeResponse:
    status_code = 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"models": [], "data": []}


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Base SQLite vierge par test + purge des variables AGENT_*/HF/OPENROUTER_*."""
    db_path = str(tmp_path / "agent_settings.db")
    agent_settings_module.reset_store_for_tests(db_path)
    for var in (
        "AGENT_PROVIDER",
        "AGENT_MODEL_NAME",
        "AGENT_OPENROUTER_URL",
        "AGENT_HF_URL",
        "AGENT_LM_STUDIO_URL",
        "AGENT_TIMEOUT_SECONDS",
        "AGENT_CONTEXT_LENGTH",
        "OPENROUTER_API_KEY",
        "HF_API_KEY",
        "HF_TOKEN",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# --- LLMClient en mode lm_studio --------------------------------------------------------


def test_lm_studio_payload_format_without_auth(monkeypatch):
    """Payload compatible OpenAI, aucune entête Authorization (serveur local)."""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, stream=True):
        captured.update(url=url, payload=json, headers=headers)
        return FakeSSEResponse(
            ['data: {"choices": [{"delta": {"content": "Bonjour"}}]}', "data: [DONE]"]
        )

    monkeypatch.setattr(llm_module.requests, "post", fake_post)
    cli = LLMClient(
        DEFAULT_LM_STUDIO_URL,
        "qwen2.5-7b-instruct",
        provider="lm_studio",
        api_key=None,
        temperature=0.4,
        context_length=4096,
        think=True,  # ne doit PAS émettre « think » côté lm_studio
    )
    content = cli.call([{"role": "user", "content": "Salut"}])

    assert content == "Bonjour"
    assert captured["url"] == DEFAULT_LM_STUDIO_URL
    assert not (captured["headers"] or {}).get("Authorization")
    payload = captured["payload"]
    assert payload["model"] == "qwen2.5-7b-instruct"
    assert payload["stream"] is True
    assert payload["temperature"] == 0.4
    assert "options" not in payload
    assert "think" not in payload


def test_lm_studio_provider_known():
    assert "lm_studio" in llm_module.PROVIDERS
    cli = LLMClient(DEFAULT_LM_STUDIO_URL, "m", provider="lm_studio")
    assert cli.provider == "lm_studio"


# --- core.agent_cache côté lm_studio -----------------------------------------------------


def test_agent_config_selects_lm_studio_provider(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "lm_studio")

    cfg = agent_cache.agent_config()
    assert cfg["provider"] == "lm_studio"
    # Modèle vide par défaut : LM Studio sert le modèle chargé dans son UI.
    assert cfg["model"] == agent_cache.DEFAULT_LM_STUDIO_MODEL_NAME
    assert cfg["lm_studio_url"] == DEFAULT_LM_STUDIO_URL


def test_llm_endpoint_lm_studio_needs_no_api_key(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "lm_studio")

    url, api_key = agent_cache._llm_endpoint(agent_cache.agent_config())
    assert url == DEFAULT_LM_STUDIO_URL
    assert api_key is None


def test_lm_studio_chat_url_normalization():
    assert agent_cache._lm_studio_chat_url("") == DEFAULT_LM_STUDIO_URL
    assert agent_cache._lm_studio_chat_url(None) == DEFAULT_LM_STUDIO_URL
    assert (
        agent_cache._lm_studio_chat_url(DEFAULT_LM_STUDIO_ROOT) == DEFAULT_LM_STUDIO_URL
    )
    assert (
        agent_cache._lm_studio_chat_url(DEFAULT_LM_STUDIO_ROOT + "/")
        == DEFAULT_LM_STUDIO_URL
    )
    assert agent_cache._lm_studio_chat_url(DEFAULT_LM_STUDIO_URL) == DEFAULT_LM_STUDIO_URL


def test_build_runner_passes_lm_studio_provider(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "lm_studio")

    captured = {}

    class FakeLLM:
        def __init__(self, url, model, **kwargs):
            captured.update(url=url, model=model, **kwargs)

    monkeypatch.setattr(agent_cache, "LLMClient", FakeLLM)
    agent_cache._build_runner("qwen2.5-0.5b-instruct")

    assert captured["url"] == DEFAULT_LM_STUDIO_URL
    assert captured["model"] == "qwen2.5-0.5b-instruct"
    assert captured["provider"] == "lm_studio"
    assert captured["api_key"] is None


def test_list_models_lm_studio_maps_ids(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "lm_studio")
    monkeypatch.setenv("AGENT_MODEL_NAME", "qwen2.5-0.5b-instruct")

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured.update(url=url, headers=headers)
        return FakeJSONResponse(
            {"data": [{"id": "b/model"}, {"id": "qwen2.5-0.5b-instruct"}, {"id": ""}]}
        )

    monkeypatch.setattr(agent_cache.requests, "get", fake_get)
    result = agent_cache.list_llm_models()

    assert captured["url"] == "http://192.168.184:1234/v1/models"
    assert captured["headers"] is None
    assert result["active"] == "qwen2.5-0.5b-instruct"
    names = [entry["name"] for entry in result["models"]]
    assert names == ["b/model", "qwen2.5-0.5b-instruct"]
    by_name = {entry["name"]: entry for entry in result["models"]}
    assert by_name["qwen2.5-0.5b-instruct"]["is_default"] is True


# --- core.agent_settings : validation & persistance ---------------------------------------


def test_validate_accepts_lm_studio_provider():
    assert agent_settings_module.validate_agent_settings({"provider": "lm_studio"}) == []
    errors = agent_settings_module.validate_agent_settings({"provider": "mistral"})
    assert any("provider" in error for error in errors)


def test_put_settings_persists_lm_studio_url(monkeypatch):
    import api.routes.agent as agent_routes

    reloaded = []
    monkeypatch.setattr(agent_routes, "reload_agent_runner", lambda: reloaded.append(1))

    resp = client.put(
        "/api/agent/settings",
        headers=HEADERS,
        json={"provider": "lm_studio", "lm_studio_url": DEFAULT_LM_STUDIO_ROOT},
    )
    assert resp.status_code == 200
    assert resp.json()["reload_ok"] is True
    assert reloaded == [1]

    body = agent_cache.get_agent_settings()
    assert body["provider"]["value"] == "lm_studio"
    assert body["lm_studio_url"]["value"] == DEFAULT_LM_STUDIO_ROOT


# --- POST /api/agent/settings/test : sonde LM Studio ---------------------------------------


def test_test_endpoint_lm_studio_probes_models_without_auth(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured.update({"url": url, "headers": headers})
        return FakeProbeResponse()

    monkeypatch.setattr("api.routes.agent.requests.get", fake_get)

    resp = client.post(
        "/api/agent/settings/test",
        headers=HEADERS,
        json={"provider": "lm_studio", "lm_studio_url": DEFAULT_LM_STUDIO_ROOT},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "LM Studio" in body["detail"]
    assert captured["url"] == f"{DEFAULT_LM_STUDIO_ROOT}/models"
    assert captured["headers"] is None  # serveur local : aucune authentification


def test_test_endpoint_lm_studio_unreachable_hint(monkeypatch):
    import requests as requests_lib

    def fake_get(url, headers=None, timeout=None):
        raise requests_lib.exceptions.ConnectionError("refusé")

    monkeypatch.setattr("api.routes.agent.requests.get", fake_get)

    resp = client.post(
        "/api/agent/settings/test",
        headers=HEADERS,
        json={"provider": "lm_studio"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "LM Studio" in body["detail"]


# --- Noyau v2 : factory + HttpLLMClient -----------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    from app.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_build_core_lm_studio_uses_local_url(monkeypatch) -> None:
    """Le noyau v2 pointe sur agent_lm_studio_url, sans clé API."""
    from app.agent import factory
    from app.config.settings import get_settings

    monkeypatch.setenv("AGENT_PROVIDER", "lm_studio")
    monkeypatch.delenv("AGENT_LM_STUDIO_URL", raising=False)

    core = factory.build_agent_core()
    assert core._llm.url == get_settings().agent_lm_studio_url
    assert core._llm.model == get_settings().agent_model_name


def test_v2_build_payload_lm_studio_openai_compatible():
    from app.infrastructure.llm.http_client import _build_payload

    p = _build_payload(
        "lm_studio", "m", [], temperature=0.8, context_length=2048, think=True
    )
    assert p["stream"] is True
    assert p["temperature"] == 0.8
    assert "options" not in p
    assert "think" not in p


def test_call_stream_lm_studio_sse_reasoning():
    """Flux SSE compatible OpenAI : contenu + trace de raisonnement `reasoning`."""
    from app.infrastructure.llm.http_client import HttpLLMClient

    body = (
        "data: {\"choices\":[{\"delta\":{\"content\":\"Bon\"}}]}\n"
        "data: {\"choices\":[{\"delta\":{\"reasoning\":\"réfl\"}}]}\n"
        "data: {\"choices\":[{\"delta\":{\"content\":\"jour\"}}]}\n"
        "data: [DONE]\n"
    ).encode()

    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    llm = HttpLLMClient(
        url=DEFAULT_LM_STUDIO_URL,
        model="qwen2.5-7b-instruct",
        provider="lm_studio",
        transport=httpx.MockTransport(handler),
    )
    thinking_events: list[str] = []
    out = llm.call_stream(
        [{"role": "user", "content": "q"}],
        on_thinking=thinking_events.append,
    )
    assert out == "Bonjour"
    assert "".join(thinking_events) == "réfl"
    assert llm.last_thinking == "réfl"
    # Fil compatible OpenAI sur le réseau : ni « options » ni « think ».
    assert "options" not in payloads[0]
    assert "think" not in payloads[0]