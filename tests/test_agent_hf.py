"""
Tests offline de l'intégration du provider Hugging Face (« hf ») dans l'agent IA.

Miroir de tests/test_agent_openrouter.py : payload compatible OpenAI, entête
Bearer, sélection du provider, clé HF requise, fabrique de runners, liste des
modèles GET /v1/models. Aucun appel réseau.
Lance avec : pytest tests/test_agent_hf.py -v
"""

import os
import tempfile

os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("AGENT_OLLAMA_URL", "http://127.0.0.1:9/api/chat")

_SETTINGS_DB = os.path.join(
    tempfile.mkdtemp(prefix="tt-settings-hf-"), "agent_settings.db"
)
os.environ.setdefault("AGENT_SETTINGS_PATH", _SETTINGS_DB)

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from core import agent_cache  # noqa: E402
from core import agent_settings as agent_settings_module  # noqa: E402

agent_settings_module.reset_store_for_tests(_SETTINGS_DB)

from ia.agent import llm_client as llm_module  # noqa: E402
from ia.agent.llm_client import LLMClient  # noqa: E402


DEFAULT_HF_URL = "https://router.huggingface.co/v1/chat/completions"


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


# --- LLMClient en mode hf --------------------------------------------------------------


def test_hf_payload_format_and_bearer_header(monkeypatch):
    """Payload compatible OpenAI : température racine, ni « options » ni « think »."""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, stream=True):
        captured.update(url=url, payload=json, headers=headers)
        return FakeSSEResponse(
            ['data: {"choices": [{"delta": {"content": "Bonjour"}}]}', "data: [DONE]"]
        )

    monkeypatch.setattr(llm_module.requests, "post", fake_post)
    client = LLMClient(
        DEFAULT_HF_URL,
        "meta-llama/Llama-3.1-8B-Instruct",
        provider="hf",
        api_key="hf_token",
        temperature=0.4,
        context_length=4096,
        think=True,  # ne doit PAS émettre « think » côté hf
    )
    content = client.call([{"role": "user", "content": "Salut"}])

    assert content == "Bonjour"
    assert captured["url"] == DEFAULT_HF_URL
    assert captured["headers"]["Authorization"] == "Bearer hf_token"
    payload = captured["payload"]
    assert payload["model"] == "meta-llama/Llama-3.1-8B-Instruct"
    assert payload["stream"] is True
    assert payload["temperature"] == 0.4
    assert "options" not in payload
    assert "think" not in payload


def test_hf_provider_known():
    assert "hf" in llm_module.PROVIDERS
    client = LLMClient(DEFAULT_HF_URL, "m", provider="hf", api_key="hf_x")
    assert client.provider == "hf"


# --- core.agent_cache côté hf ----------------------------------------------------------


def test_agent_config_selects_hf_provider(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "hf")
    monkeypatch.delenv("AGENT_HF_URL", raising=False)
    monkeypatch.delenv("HF_API_KEY", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)

    cfg = agent_cache.agent_config()
    assert cfg["provider"] == "hf"
    assert cfg["model"] == agent_cache.DEFAULT_HF_MODEL_NAME
    assert cfg["hf_url"] == DEFAULT_HF_URL


def test_llm_endpoint_requires_api_key_for_hf(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "hf")
    monkeypatch.delenv("HF_API_KEY", raising=False)
    monkeypatch.delenv("HF_TOKEN", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        agent_cache._llm_endpoint(agent_cache.agent_config())
    assert exc_info.value.status_code == 500
    assert "HF_API_KEY" in exc_info.value.detail


def test_llm_endpoint_hf_env_fallback_hf_token(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "hf")
    monkeypatch.delenv("HF_API_KEY", raising=False)
    monkeypatch.setenv("HF_TOKEN", "hf_env_token")

    url, api_key = agent_cache._llm_endpoint(agent_cache.agent_config())
    assert url == DEFAULT_HF_URL
    assert api_key == "hf_env_token"


def test_build_runner_passes_hf_provider_and_key(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "hf")
    monkeypatch.setenv("HF_API_KEY", "hf_build")
    monkeypatch.delenv("AGENT_HF_URL", raising=False)

    captured = {}

    class FakeLLM:
        def __init__(self, url, model, **kwargs):
            captured.update(url=url, model=model, **kwargs)

    monkeypatch.setattr(agent_cache, "LLMClient", FakeLLM)
    agent_cache._build_runner("vendor/model")

    assert captured["url"] == DEFAULT_HF_URL
    assert captured["model"] == "vendor/model"
    assert captured["provider"] == "hf"
    assert captured["api_key"] == "hf_build"


def test_list_models_hf_maps_ids(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "hf")
    monkeypatch.setenv("AGENT_MODEL_NAME", "a/model")
    monkeypatch.setenv("HF_API_KEY", "hf_list")
    monkeypatch.delenv("AGENT_HF_URL", raising=False)

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured.update(url=url, headers=headers)
        return FakeJSONResponse(
            {"data": [{"id": "b/model"}, {"id": "a/model"}, {"id": ""}]}
        )

    monkeypatch.setattr(agent_cache.requests, "get", fake_get)
    result = agent_cache.list_llm_models()

    assert captured["url"] == "https://router.huggingface.co/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer hf_list"
    assert result["active"] == "a/model"
    names = [entry["name"] for entry in result["models"]]
    assert names == ["a/model", "b/model"]
    by_name = {entry["name"]: entry for entry in result["models"]}
    assert by_name["a/model"]["is_default"] is True


def test_hf_chat_url_normalization():
    assert agent_cache._hf_chat_url("") == DEFAULT_HF_URL
    assert agent_cache._hf_chat_url(None) == DEFAULT_HF_URL
    assert (
        agent_cache._hf_chat_url("https://router.huggingface.co/v1")
        == DEFAULT_HF_URL
    )
    assert agent_cache._hf_chat_url(DEFAULT_HF_URL) == DEFAULT_HF_URL
