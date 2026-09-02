# project/tests/test_app_settings.py
"""Tests de la configuration centralisée (app/config/settings.py)."""

import pytest


@pytest.fixture()
def fresh_settings(monkeypatch):
    """Settings reconstruites dans un environnement contrôlé."""
    from app.config.settings import get_settings

    get_settings.cache_clear()
    yield None
    get_settings.cache_clear()


def test_defaults(fresh_settings) -> None:
    from app.config.settings import AgentProvider, get_settings

    s = get_settings()
    assert s.agent_provider is AgentProvider.OLLAMA
    assert s.agent_max_llm_rounds == 6
    assert s.effective_ws_token == s.api_key
    assert s.active_flags() == {
        "reliability": False,
        "audit": False,
        "tool_analytics": False,
        "context": False,
        "copilot": False,
        "websocket": False,
        "multi_agent": False,
    }


def test_flags_from_env(monkeypatch, fresh_settings) -> None:
    from app.config.settings import get_settings

    monkeypatch.setenv("AGENT_AUDIT", "1")
    monkeypatch.setenv("AGENT_Copilot", "true")  # insensible à la casse
    s = get_settings()
    assert s.flag_audit is True
    assert s.flag_copilot is True
    assert s.flag_websocket is False


def test_openrouter_requires_key(monkeypatch, fresh_settings) -> None:
    from app.config.settings import get_settings

    monkeypatch.setenv("AGENT_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(Exception, match="OPENROUTER_API_KEY"):
        get_settings()


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
