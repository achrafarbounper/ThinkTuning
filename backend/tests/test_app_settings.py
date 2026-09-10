# project/tests/test_app_settings.py
"""Tests de la configuration d'infrastructure (app/config/settings.py) et de la
configuration de l'agent (module IHM : app/agent/settings.AgentConfig).

SCRUM-138 : ``app/config/settings.py`` ne porte plus AUCUNE configuration
d'agent (provider LLM, URLs, clés, budgets, flags, MCP). Ces réglages vivent
dans le module de configuration de l'IHM — valeurs ENTièrement stockées et
chargées depuis la base de persistance (MongoDB via ``MongoAgentSettingsStore``
dès ``PERSISTENCE_BACKEND=mongodb`` ; store SQLite de dev sinon) via le port
``AgentSettingsPort``. La base est isolée par test (cf. tests/conftest.py) et
la couche env (AGENT_*, MCP_*) est nettoyée pour rester déterministe.
"""

import pytest

# Variables d'environnement de la configuration agent / MCP : retirées pour
# que chaque test parte d'un environnement propre (un ``.env`` local ou des
# variables machine ne doivent pas faire flakker les tests).
_AGENT_ENV_KEYS = (
    "AGENT_PROVIDER",
    "AGENT_MODEL_NAME",
    "AGENT_MAX_LLM_ROUNDS",
    "AGENT_MAX_TOOL_CALLS",
    "AGENT_LOG_LEVEL",
    "AGENT_NEW_CORE",
    "AGENT_LLM_V2",
    "AGENT_RELIABILITY",
    "AGENT_AUDIT",
    "AGENT_TOOL_ANALYTICS",
    "AGENT_CONTEXT",
    "AGENT_COPILOT",
    "AGENT_WEBSOCKET",
    "AGENT_MULTI_AGENT",
    "AGENT_CUSTOM_TOOLS",
    "MCP_FIRST",
    "MCP_AUTH_REQUIRED",
)

_SETTINGS_ENV_KEYS = (
    "API_KEY",
    "OPENROUTER_API_KEY",
    "HF_API_KEY",
    "HF_TOKEN",
    "DASHBOARD_WS_TOKEN",
)


class FakeSettingsPort:
    """Port ``AgentSettingsPort`` en mémoire pour les tests sans I/O."""

    def __init__(self, persisted: dict | None = None) -> None:
        self._persisted: dict = dict(persisted or {})

    def get_all(self) -> dict:
        return dict(self._persisted)

    def save_many(self, values: dict) -> dict:
        self._persisted.update(values)
        return values


@pytest.fixture()
def fresh_env(monkeypatch):
    """Environnement nettoyé de toute variable agent / MCP / secrets."""
    for key in (*_AGENT_ENV_KEYS, *_SETTINGS_ENV_KEYS):
        monkeypatch.delenv(key, raising=False)
    yield None


@pytest.fixture()
def fresh_settings(monkeypatch, fresh_env):
    """Settings d'infrastructure reconstruites dans un environnement contrôlé."""
    from app.config.settings import get_settings

    monkeypatch.setenv("API_KEY", "secret-key")
    get_settings.cache_clear()
    yield None
    get_settings.cache_clear()


# --- app/config/settings.py : plus AUCUNE configuration d'agent -------------


def test_settings_expose_no_agent_config(fresh_env) -> None:
    """Garde-fou SCRUM-138 : Settings ne porte AUCUN paramètre d'agent."""
    from app.config import settings as settings_module
    from app.config.settings import get_settings

    assert not hasattr(settings_module, "AgentProvider")
    settings = get_settings(env_file=None)
    for forbidden in (
        "agent_provider",
        "agent_model_name",
        "agent_ollama_url",
        "agent_openrouter_url",
        "agent_hf_url",
        "agent_lm_studio_url",
        "openrouter_api_key",
        "hf_api_key",
        "hf_token",
        "agent_timeout_seconds",
        "agent_context_length",
        "agent_max_llm_rounds",
        "agent_max_tool_calls",
        "agent_log_level",
        "flag_new_core",
        "flag_llm_v2",
        "mcp_first",
        "mcp_auth_required",
    ):
        assert not hasattr(settings, forbidden), forbidden


def test_settings_keep_infrastructure_fields(fresh_settings) -> None:
    """Les réglages d'infrastructure restent portés par Settings."""
    from app.config.settings import get_settings

    settings = get_settings(env_file=None)
    assert settings.persistence_backend in ("sqlite", "mongodb")
    assert settings.effective_ws_token == "secret-key"
    assert settings.train_stream_stall_minutes >= 1
    assert 0.0 <= settings.model_sanity_min_confidence <= 1.0


def test_flags_from_env_are_gone(fresh_env) -> None:
    """Les flags agent ne sont plus pilotés par Settings (module IHM)."""
    from app.config.settings import get_settings

    monkeypatch_env_keys = _AGENT_ENV_KEYS
    assert not hasattr(get_settings(env_file=None), "flag_websocket")
    assert monkeypatch_env_keys  # la liste de nettoyage reste documentée


# --- AgentConfig : défauts / env / base (module IHM) -------------------------


def test_agent_config_defaults(fresh_env) -> None:
    """Sans base ni env : défauts historiques du module IHM."""
    from app.agent.settings import AgentProvider, get_agent_config

    config = get_agent_config(FakeSettingsPort())
    assert config.provider is AgentProvider.OLLAMA
    assert config.model_name == "openrouter/free"
    assert config.max_llm_rounds == 6
    assert config.max_tool_calls == 20
    assert config.log_level == "INFO"
    assert config.mcp_first is False
    assert config.mcp_auth_required is True
    assert config.active_flags() == {
        "reliability": True,
        "audit": True,
        "tool_analytics": True,
        "context": True,
        "copilot": True,
        "websocket": True,
        "multi_agent": True,
        "custom_tools": True,
        "new_core": True,
        "llm_v2": True,
    }


def test_ws_token_fallback(monkeypatch, fresh_env) -> None:
    from app.config.settings import get_settings

    monkeypatch.delenv("DASHBOARD_WS_TOKEN", raising=False)
    monkeypatch.setenv("API_KEY", "secret-key")
    settings = get_settings(env_file=None)
    assert settings.effective_ws_token == "secret-key"


def test_agent_config_env_overrides(monkeypatch, fresh_env) -> None:
    """L'environnement reste un repli de compatibilité (CI / déploiements)."""
    from app.agent.settings import get_agent_config

    monkeypatch.setenv("AGENT_WEBSOCKET", "0")
    monkeypatch.setenv("AGENT_MAX_LLM_ROUNDS", "3")
    monkeypatch.setenv("AGENT_LOG_LEVEL", "debug")
    config = get_agent_config(FakeSettingsPort())
    assert config.flag_websocket is False
    assert config.max_llm_rounds == 3
    assert config.log_level == "DEBUG"


def test_agent_config_persisted_overrides_env(monkeypatch, fresh_env) -> None:
    """La base (MongoDB en production) est TOUJOURS prioritaire sur l'env."""
    from app.agent.settings import AgentProvider, get_agent_config

    monkeypatch.setenv("AGENT_PROVIDER", "ollama")
    port = FakeSettingsPort(
        {"provider": "openrouter", "openrouter_api_key": "sk-or-v1-test"}
    )
    config = get_agent_config(port)
    assert config.provider is AgentProvider.OPENROUTER
    assert config.openrouter_api_key == "sk-or-v1-test"


def test_agent_config_openrouter_requires_key(monkeypatch, fresh_env) -> None:
    """Fail-fast préservé : provider openrouter sans clé → ValueError explicite."""
    from app.agent.settings import get_agent_config

    monkeypatch.setenv("AGENT_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        get_agent_config(FakeSettingsPort())


def test_agent_config_hf_token_fallback(monkeypatch, fresh_env) -> None:
    """Repli historique : env HF_TOKEN alimente la clé HF effective."""
    from app.agent.settings import get_agent_config

    monkeypatch.setenv("AGENT_PROVIDER", "hf")
    monkeypatch.setenv("HF_TOKEN", "hf_xxx")
    config = get_agent_config(FakeSettingsPort())
    assert config.effective_hf_key == "hf_xxx"


def test_agent_flag_helper(monkeypatch, fresh_env) -> None:
    """``agent_flag`` : lecture live (base > env > défaut), False si inconnu."""
    from app.agent.settings import agent_flag

    assert agent_flag("context") is True
    monkeypatch.setenv("AGENT_CONTEXT", "0")
    assert agent_flag("context") is False
    assert agent_flag("inconnu") is False


def test_agent_flag_persisted(monkeypatch, fresh_env) -> None:
    """Un flag POSÉ en base l'emporte sur l'environnement (dashboard effectif)."""
    from app.agent.settings import agent_flag

    monkeypatch.setenv("AGENT_COPILOT", "1")
    port = FakeSettingsPort({"flag_copilot": False})
    assert agent_flag("copilot", port) is False
