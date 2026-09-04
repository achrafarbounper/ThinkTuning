# project/tests/test_app_settings.py
"""Tests de la configuration centralisée (app/config/settings.py)."""

import pytest


# Variables d'environnement qui peuvent impacter les défauts testés : on les
# retire pour que chaque test parte d'un environnement propre (un ``.env``
# local ou des variables machine ne doivent pas faire flakker les tests).
_SETTINGS_ENV_KEYS = (
    "API_KEY",
    "AGENT_PROVIDER",
    "AGENT_MODEL_NAME",
    "AGENT_MAX_LLM_ROUNDS",
    "AGENT_NEW_CORE",
    "AGENT_LLM_V2",
    "OPENROUTER_API_KEY",
    "HF_API_KEY",
    "HF_TOKEN",
    "DASHBOARD_WS_TOKEN",
)


@pytest.fixture()
def fresh_settings(monkeypatch):
    """Settings reconstruites dans un environnement contrôlé.

    Le provider par défaut étant OpenRouter (fail-fast), une clé dummy est
    posée par défaut comme le ferait le ``.env`` de prod ; les tests qui
    testent l'absence de clé la retirent explicitement.
    """
    from app.config.settings import get_settings

    for key in _SETTINGS_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")
    get_settings.cache_clear()
    yield None
    get_settings.cache_clear()


def test_defaults(fresh_settings, monkeypatch) -> None:
    from app.config.settings import AgentProvider, get_settings

    # Le provider par défaut est OpenRouter (fail-fast) : on fournit uniquement
    # la clé qu'il exige, sans surcharger les autres défauts.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test")

    s = get_settings()
    assert s.agent_provider is AgentProvider.OPENROUTER
    assert s.agent_model_name == "openrouter/free"
    assert s.agent_max_llm_rounds == 6
    assert s.effective_ws_token == s.api_key
    assert s.active_flags() == {
        "reliability": True,
        "audit": True,
        "tool_analytics": True,
        "context": True,
        "copilot": True,
        "websocket": True,
        "multi_agent": True,
        "new_core": True,
        "llm_v2": True,
    }


def test_flags_from_env(monkeypatch, fresh_settings) -> None:
    from app.config.settings import get_settings

    monkeypatch.setenv("AGENT_AUDIT", "1")
    monkeypatch.setenv("AGENT_Copilot", "true")  # insensible à la casse
    monkeypatch.setenv("AGENT_WEBSOCKET", "0")   # désactivation explicite
    s = get_settings()
    assert s.flag_audit is True
    assert s.flag_copilot is True
    assert s.flag_websocket is False


def test_openrouter_requires_key(monkeypatch, fresh_settings) -> None:
    from app.config.settings import get_settings

    monkeypatch.setenv("AGENT_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # env_file=None : ignore tout .env local qui définirait la clé (sinon
    # le test serait fiable uniquement sur les machines sans .env).
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        get_settings(env_file=None)


def test_hf_fallback_token(monkeypatch, fresh_settings) -> None:
    from app.config.settings import get_settings

    monkeypatch.setenv("AGENT_PROVIDER", "hf")
    monkeypatch.delenv("HF_API_KEY", raising=False)
    monkeypatch.setenv("HF_TOKEN", "hf_xxx")
    s = get_settings()
    assert s.effective_hf_key == "hf_xxx"


def test_ws_token_fallback(monkeypatch, fresh_settings) -> None:
    from app.config.settings import get_settings

    monkeypatch.delenv("DASHBOARD_WS_TOKEN", raising=False)
    monkeypatch.setenv("API_KEY", "secret-key")
    s = get_settings()
    assert s.effective_ws_token == "secret-key"
