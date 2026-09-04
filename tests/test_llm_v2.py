"""Tests de la bascule ``AGENT_LLM_V2`` et de la conformité ``LLMClientPort``.

Objectif : prouver que le client LLM réside derrière un port unique, que
l'implémentation v2 (stub déterministe) et l'implémentation legacy ont le même
contrat, et que le seam d'usine (``build_llm_client``) bascule via le flag sans
changer les signatures.
"""

from __future__ import annotations

import inspect

from app.agent.factory import (
    AgentProvider,
    build_llm_client,
    llm_v2_enabled,
)
from app.domain.ports import LLMClientPort
from app.infrastructure.llm import HttpLLMClient, StubLLMClient

LLM_METHODS = ("call", "call_stream")


def test_stub_is_llm_client_port():
    assert issubclass(StubLLMClient, LLMClientPort)


def test_stub_implements_full_port_signature():
    for name in LLM_METHODS:
        port_params = set(inspect.signature(getattr(LLMClientPort, name)).parameters)
        impl_params = set(inspect.signature(getattr(StubLLMClient, name)).parameters)
        assert port_params <= impl_params, (
            f"StubLLMClient.{name} : {port_params - impl_params}"
        )


def test_legacy_client_is_llm_client_port():
    """Le client legacy satisfait lui aussi le port (contrat unique avéré).

    Vérifie la CLASSE (pas l'instanciation) : éviter de dépendre des Settings
    (provider/API key) — la conformité structurelle ne requiert aucun réseau.
    """
    from ia.agent.llm_client import LLMClient

    assert issubclass(LLMClient, LLMClientPort)


def test_stub_call_and_call_stream_deterministic():
    client = StubLLMClient(response="hello")
    assert client.call([{"role": "user", "content": "x"}]) == "hello"
    received: list[str] = []
    out = client.call_stream(
        [{"role": "user", "content": "x"}], on_content=received.append
    )
    assert out == "hello"
    assert received == ["hello"]


def test_llm_v2_enabled_flag_selection(monkeypatch):
    monkeypatch.delenv("AGENT_LLM_V2", raising=False)
    # Activé par défaut (bascule v2 en production) ; AGENT_LLM_V2=0 = repli.
    assert llm_v2_enabled() is True
    monkeypatch.setenv("AGENT_LLM_V2", "0")
    assert llm_v2_enabled() is False
    monkeypatch.setenv("AGENT_LLM_V2", "1")
    assert llm_v2_enabled() is True


def test_build_llm_client_routes_by_flag(monkeypatch):
    """Le seam renvoie le client v2 (HttpLLMClient) ou le client legacy selon le flag."""
    from unittest.mock import patch

    # v2 activé → HttpLLMClient (patche Settings+endpoint : pas de `.env`).
    monkeypatch.setenv("AGENT_LLM_V2", "1")
    with patch("app.agent.factory.get_settings") as mock_settings, \
         patch("app.agent.factory.llm_endpoint", return_value=("http://x", None)):
        mock_settings.return_value.agent_model_name = "m"
        mock_settings.return_value.agent_provider = AgentProvider.OLLAMA
        mock_settings.return_value.agent_timeout_seconds = 30
        mock_settings.return_value.agent_context_length = 2048
        client = build_llm_client()

    assert isinstance(client, HttpLLMClient)

    # v2 désactivé → délègue à l'implémentation legacy (repli intact).
    sentinel = object()
    monkeypatch.setenv("AGENT_LLM_V2", "0")
    with patch("app.agent.factory.build_legacy_llm_client", return_value=sentinel):
        assert build_llm_client() is sentinel


def test_build_llm_client_defaults_to_v2(monkeypatch):
    """Sans ``AGENT_LLM_V2`` (défaut de production), le seam renvoie le v2."""
    from unittest.mock import patch

    monkeypatch.delenv("AGENT_LLM_V2", raising=False)
    with patch("app.agent.factory.get_settings") as mock_settings, \
         patch("app.agent.factory.llm_endpoint", return_value=("http://x", None)):
        mock_settings.return_value.agent_model_name = "m"
        mock_settings.return_value.agent_provider = AgentProvider.OLLAMA
        mock_settings.return_value.agent_timeout_seconds = 30
        mock_settings.return_value.agent_context_length = 2048
        client = build_llm_client()

    assert isinstance(client, HttpLLMClient)
