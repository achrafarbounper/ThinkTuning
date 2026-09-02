# project/tests/test_agent_factory.py
"""Tests de la factory du noyau agentique (sans aucun appel réseau)."""

import pytest

from app.agent import factory
from app.agent.core import AgentCore
from app.config.settings import get_settings
from app.domain.ports import ToolRegistryPort


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_flag_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_NEW_CORE", raising=False)
    assert factory.new_core_enabled() is False


def test_flag_enabled(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_NEW_CORE", "1")
    assert factory.new_core_enabled() is True


def test_build_agent_core_wires_real_legacy_parts(monkeypatch) -> None:
    """Le noyau assemblé utilise le registre legacy et le client legacy."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("AGENT_PROVIDER", "ollama")
    core = factory.build_agent_core()
    assert isinstance(core, AgentCore)
    # Le registre réel expose les outils métier documentés.
    names = core._registry.tool_names()
    assert "read_file" in names and "predict_sentiment" in names
    assert isinstance(core._registry, ToolRegistryPort)
    # Le client LLM legacy est bien le module historique.
    from ia.agent.llm_client import LLMClient

    assert isinstance(core._llm, LLMClient)
    assert core._llm.model == get_settings().agent_model_name


def test_build_core_openrouter_uses_key_and_url(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    core = factory.build_agent_core()
    assert core._llm.url == get_settings().agent_openrouter_url
    assert core._llm.model == get_settings().agent_model_name


def test_build_core_respects_budget_settings(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_MAX_LLM_ROUNDS", "3")
    monkeypatch.setenv("AGENT_MAX_TOOL_CALLS", "7")
    core = factory.build_agent_core()
    assert core._max_tool_calls == 7
    # max_rounds est lu par run() depuis Intent, mais Settings porte la valeur.
    assert get_settings().agent_max_llm_rounds == 3
