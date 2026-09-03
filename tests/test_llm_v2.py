"""Tests de la bascule ``AGENT_LLM_V2`` et de la conformité ``LLMClientPort``.

Objectif : prouver que le client LLM réside derrière un port unique, que
l'implémentation v2 (stub déterministe) et l'implémentation legacy ont le même
contrat, et que le seam d'usine (``build_llm_client``) bascule via le flag sans
changer les signatures.
"""

from __future__ import annotations

import inspect

from app.agent.factory import build_llm_client, llm_v2_enabled
from app.domain.ports import LLMClientPort
from app.infrastructure.llm import StubLLMClient

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
    # Désactivé par défaut (rollout sûr) ; activé via env.
    assert llm_v2_enabled() is False
    monkeypatch.setenv("AGENT_LLM_V2", "1")
    assert llm_v2_enabled() is True
    monkeypatch.setenv("AGENT_LLM_V2", "0")
    assert llm_v2_enabled() is False


def test_build_llm_client_routes_by_flag(monkeypatch):
    """Le seam renvoie le stub (v2) ou le client legacy selon le flag."""
    from unittest.mock import patch

    monkeypatch.setenv("AGENT_LLM_V2", "1")
    client = build_llm_client()
    assert isinstance(client, StubLLMClient)

    # v2 désactivé → délègue à l'implémentation legacy (chemin intact).
    sentinel = object()
    monkeypatch.setenv("AGENT_LLM_V2", "0")
    with patch("app.agent.factory.build_legacy_llm_client", return_value=sentinel):
        assert build_llm_client() is sentinel
