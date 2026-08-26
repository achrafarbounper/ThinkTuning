"""
Tests offline de l'intégration du provider OpenRouter dans l'agent IA.

Couvre :
    - ia/agent/llm_client.LLMClient en mode ``provider="openrouter"`` :
      payload compatible OpenAI (pas de bloc « options », pas de « think »),
      entête Authorization Bearer, flux SSE parsé (delta.reasoning,
      delta.reasoning_content en repli), callbacks temps réel, repli
      balises <think> inline ;
    - validation du nom de provider (valeur inconnue refusée) ;
    - core.agent_cache : sélection du provider par AGENT_PROVIDER,
      clé OPENROUTER_API_KEY requise, fabrique de runners, liste des
      modèles GET /api/v1/models mappée sur le contrat Ollama.

Aucun appel réseau : requests.post / requests.get sont remplacés.
Lance avec : pytest tests/test_agent_openrouter.py -v
"""

import os
import tempfile

# Config test AVANT tout import (le cache insère ia/ dans sys.path).
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("AGENT_OLLAMA_URL", "http://127.0.0.1:9/api/chat")  # port factice

# Isolation des paramètres persistés : ces tests supposent une config « env
# uniquement » ; on pointe la base SQLite vers un fichier temporaire vide pour
# ne jamais lire/écrire experiments/agent_settings.db.
_SETTINGS_DB = os.path.join(
    tempfile.mkdtemp(prefix="tt-settings-openrouter-"), "agent_settings.db"
)
os.environ.setdefault("AGENT_SETTINGS_PATH", _SETTINGS_DB)

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

# ORDRE IMPORTANT : importer agent_cache AVANT tout module « agent.* »,
# c'est lui qui ajoute le dossier ia/ au sys.path.
from core import agent_cache  # noqa: E402
from core import agent_settings as agent_settings_module  # noqa: E402

agent_settings_module.reset_store_for_tests(_SETTINGS_DB)

from agent import llm_client as llm_module  # noqa: E402
from agent.llm_client import LLMClient  # noqa: E402


DEFAULT_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class FakeSSEResponse:
    """Réponse requests factice diffusant un flux SSE ligne à ligne."""

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
    """Réponse requests factice d'un endpoint JSON (liste des modèles)."""

    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _capture_post(monkeypatch, lines):
    """Remplace requests.post par un serveur SSE factice et capture l'appel."""
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None, stream=True):
        captured["url"] = url
        captured["payload"] = json
        captured["headers"] = headers
        return FakeSSEResponse(lines)

    monkeypatch.setattr(llm_module.requests, "post", fake_post)
    return captured


def _sse(payload: str) -> str:
    return f"data: {payload}"


# --- LLMClient en mode openrouter -----------------------------------------------------


def test_openrouter_payload_format_and_bearer_header(monkeypatch):
    """Payload compatible OpenAI : température racine, ni « options » ni « think »."""
    lines = [
        _sse('{"choices":[{"delta":{"content":"Bonjour"}}]}'),
        'data: [DONE]',
    ]
    captured = _capture_post(monkeypatch, lines)

    client = LLMClient(
        DEFAULT_OPENROUTER_URL,
        "deepseek/deepseek-r1:free",
        timeout=5,
        context_length=8192,
        provider="openrouter",
        api_key="sk-or-test",
        think=True,  # ne doit PAS produire un champ « think » côté OpenRouter
    )
    answer = client.call([{"role": "user", "content": "salut"}])

    assert answer == "Bonjour"
    assert captured["url"] == DEFAULT_OPENROUTER_URL
    payload = captured["payload"]
    assert payload["model"] == "deepseek/deepseek-r1:free"
    assert payload["stream"] is True
    assert isinstance(payload["temperature"], float)
    assert "options" not in payload  # num_ctx n'existe pas chez OpenRouter
    assert "think" not in payload
    assert captured["headers"]["Authorization"] == "Bearer sk-or-test"


def test_openrouter_streams_reasoning_then_content_in_real_time(monkeypatch):
    """delta.reasoning (et reasoning_content) alimentent on_thinking AVANT on_content."""
    lines = [
        _sse('{"choices":[{"delta":{"reasoning":"Première piste."}}]}'),
        _sse('{"choices":[{"delta":{"reasoning_content":"Complément."}}]}'),
        _sse('{"choices":[{"delta":{"content":"Bonjour"}}]}'),
        _sse('{"choices":[{"delta":{"content":" !"}}]}'),
        _sse('{"choices":[{"delta":{},"finish_reason":"stop"}]}'),
        'data: [DONE]',
    ]
    _capture_post(monkeypatch, lines)

    thinking_chunks: list[str] = []
    order: list[str] = []
    client = LLMClient(
        DEFAULT_OPENROUTER_URL, "deepseek/deepseek-r1:free", provider="openrouter",
        api_key="sk-or-test",
    )
    answer = client.call_stream(
        [{"role": "user", "content": "q"}],
        on_thinking=lambda t: (thinking_chunks.append(t), order.append("think")),
        on_content=lambda t: (order.append("content")),
    )

    assert answer == "Bonjour !"
    assert "Première piste." in client.last_thinking
    assert "Complément." in client.last_thinking
    # Tout fragment de réflexion précède tout fragment de réponse.
    assert order.index("content") > max(
        i for i, kind in enumerate(order) if kind == "think"
    )


def test_openrouter_extracts_inline_think_tags(monkeypatch):
    """Repli <think> inline : certains modèles encadrent leur raisonnement eux-mêmes."""
    lines = [
        _sse('{"choices":[{"delta":{"content":"<think>"}}]}'),
        _sse('{"choices":[{"delta":{"content":"Peser les options."}}]}'),
        _sse('{"choices":[{"delta":{"content":"</think>Réponse propre."}}]}'),
        'data: [DONE]',
    ]
    _capture_post(monkeypatch, lines)

    client = LLMClient(DEFAULT_OPENROUTER_URL, "vendor/model", provider="openrouter")
    answer = client.call([{"role": "user", "content": "q"}])

    assert answer == "Réponse propre."
    assert client.last_thinking == "Peser les options."


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError):
        LLMClient("http://x/api/chat", "m", provider="anthropic")


def test_default_provider_is_ollama():
    """Sans paramètre provider : comportement historique (Ollama) préservé."""
    client = LLMClient("http://ollama.test/api/chat", "llama3.1:8b")
    assert client.provider == "ollama"
    assert client.api_key is None


# --- core.agent_cache : configuration et fabrique --------------------------------------


def test_agent_config_selects_openrouter_provider(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "OpenRouter")  # casse insensible
    monkeypatch.delenv("AGENT_MODEL_NAME", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_OPENROUTER_URL", raising=False)

    cfg = agent_cache.agent_config()
    assert cfg["provider"] == "openrouter"
    assert cfg["model"] == agent_cache.DEFAULT_OPENROUTER_MODEL_NAME
    assert cfg["openrouter_url"] == DEFAULT_OPENROUTER_URL


def test_agent_config_defaults_to_ollama(monkeypatch):
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)
    monkeypatch.delenv("AGENT_OPENROUTER_URL", raising=False)

    cfg = agent_cache.agent_config()
    assert cfg["provider"] == "ollama"


def test_llm_endpoint_requires_api_key_for_openrouter(monkeypatch):
    monkeypatch.setenv("AGENT_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    with pytest.raises(HTTPException) as exc_info:
        agent_cache._llm_endpoint(agent_cache.agent_config())
    assert exc_info.value.status_code == 500
    assert "OPENROUTER_API_KEY" in exc_info.value.detail


def test_build_runner_passes_provider_and_key(monkeypatch):
    """La fabrique transmet provider + clé au LLMClient (branchement OpenRouter)."""
    monkeypatch.setenv("AGENT_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-build")
    monkeypatch.delenv("AGENT_OPENROUTER_URL", raising=False)

    captured = {}

    class FakeLLM:
        def __init__(self, url, model, **kwargs):
            captured["url"] = url
            captured["model"] = model
            captured.update(kwargs)

    monkeypatch.setattr(agent_cache, "LLMClient", FakeLLM)
    agent_cache._build_runner("vendor/model")

    assert captured["url"] == DEFAULT_OPENROUTER_URL
    assert captured["model"] == "vendor/model"
    assert captured["provider"] == "openrouter"
    assert captured["api_key"] == "sk-or-build"


def test_build_runner_ollama_has_no_api_key(monkeypatch):
    """Chemin historique inchangé : provider ollama, aucune authentification."""
    monkeypatch.delenv("AGENT_PROVIDER", raising=False)

    captured = {}

    class FakeLLM:
        def __init__(self, url, model, **kwargs):
            captured["url"] = url
            captured.update(kwargs)

    monkeypatch.setattr(agent_cache, "LLMClient", FakeLLM)
    agent_cache._build_runner()

    assert captured["provider"] == "ollama"
    assert captured["api_key"] is None


# --- core.agent_cache.list_llm_models côté OpenRouter ----------------------------------


def test_list_models_openrouter_maps_ids(monkeypatch):
    """GET /api/v1/models mappé sur le même contrat que la liste Ollama."""
    monkeypatch.setenv("AGENT_PROVIDER", "openrouter")
    monkeypatch.setenv("AGENT_MODEL_NAME", "a/model")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-list")

    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return FakeJSONResponse(
            {"data": [{"id": "b/model"}, {"id": "a/model"}, {"id": ""}]}
        )

    monkeypatch.setattr(agent_cache.requests, "get", fake_get)
    result = agent_cache.list_llm_models()

    assert captured["url"] == "https://openrouter.ai/api/v1/models"
    assert captured["headers"]["Authorization"] == "Bearer sk-or-list"
    assert result["active"] == "a/model"
    names = [entry["name"] for entry in result["models"]]
    assert names == ["a/model", "b/model"]  # tri alphabétique, id vide ignoré
    by_name = {entry["name"]: entry for entry in result["models"]}
    assert by_name["a/model"]["is_default"] is True
    assert by_name["b/model"]["is_default"] is False