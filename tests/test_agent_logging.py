"""Tests offline des LOGS de l'agent IA (vérifiés via caplog de pytest).

Garantit que le suivi d'exécution s'affiche correctement dans le terminal :
    - chaque tour de boucle, appel et complétion d'outil sont tracés en INFO ;
    - une auto-correction (tool inconnu, réponse sans JSON) émet un WARNING ;
    - un échec d'outil émet un WARNING contenant le TRACEBACK complet ;
    - LLMClient trace ses requêtes/réponses (avec durée) en INFO ;
    - un timeout du LLM est tracé en ERROR.

Aucun réseau : le LLM est remplacé par un ScriptedLLM déterministe et
requests.post est monkeypatché.
Lance avec : pytest tests/test_agent_logging.py -v
"""

import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IA_DIR = os.path.join(PROJECT_ROOT, "ia")
for _p in (PROJECT_ROOT, IA_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402
import requests  # noqa: E402

from agent.agent_core import AgentCore  # noqa: E402
from agent.llm_client import LLMClient  # noqa: E402
from tools import tool_registry  # noqa: E402


class ScriptedLLM:
    """LLM factice : renvoie les réponses fournies, mémorise les prompts."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def call(self, messages):
        self.prompts.append(messages[-1]["content"])
        return self.replies.pop(0)


# --- Logs d'AgentCore ----------------------------------------------------------------


def test_agentcore_logs_rounds_tool_calls_and_completion(caplog):
    """Un run nominal trace : tour de boucle, appel d'outil, fin d'outil, réponse."""
    llm = ScriptedLLM([
        '{"tool": "add", "args": {"a": 2, "b": 3}}',
        "Deux plus trois font cinq.",
    ])
    with caplog.at_level(logging.INFO, logger="agent"):
        answer = AgentCore(llm).run("calcule 2+3")

    assert answer == "Deux plus trois font cinq."
    messages = [r.getMessage() for r in caplog.records if r.name.startswith("agent")]
    # Le tour de boucle est numéroté.
    assert any("Tour 1/" in m and "LLM" in m for m in messages)
    # L'appel d'outil est tracé (départ).
    assert any("add" in m and "Exécution du tool" in m for m in messages)
    # La complétion de l'outil est tracée (avec durée).
    assert any("add" in m and "terminé" in m for m in messages)
    # La réponse finale est annoncée.
    assert any("Réponse finale" in m for m in messages)


def test_agentcore_logs_auto_correction_warning(caplog):
    """Un tool inconnu déclenche un WARNING d'auto-correction explicite."""
    llm = ScriptedLLM([
        '{"tool": "division", "args": {"a": 12, "b": 30}}',
        '{"tool": "add", "args": {"a": 12, "b": 30}}',
        "Addition effectuée.",
    ])
    with caplog.at_level(logging.INFO, logger="agent"):
        AgentCore(llm).run("calcule 12+30")

    warnings_ = [
        r.getMessage()
        for r in caplog.records
        if r.name.startswith("agent") and r.levelno == logging.WARNING
    ]
    assert any(
        "Auto-correction" in m and "Tool inconnu : 'division'" in m for m in warnings_
    ), warnings_


def test_agentcore_logs_tool_failure_with_traceback(caplog, monkeypatch):
    """Une exception dans un tool produit un WARNING AVEC traceback (exc_info)."""

    def boom():
        raise RuntimeError("explosion simulée")

    monkeypatch.setitem(tool_registry.TOOLS, "boom", boom)
    monkeypatch.setitem(tool_registry.REQUIRED_ARGS, "boom", [])

    llm = ScriptedLLM([
        '{"tool": "boom"}',
        "L'outil a échoué, je l'explique à l'utilisateur.",
    ])
    with caplog.at_level(logging.INFO, logger="agent"):
        AgentCore(llm).run("provoque une erreur")

    failures = [
        r
        for r in caplog.records
        if r.name.startswith("agent")
        and r.levelno == logging.WARNING
        and "Échec de l'outil 'boom'" in r.getMessage()
    ]
    assert failures, "aucun log d'échec d'outil émis"
    assert failures[0].exc_info is not None, "le traceback (exc_info) est manquant"
    assert "RuntimeError" in caplog.text


# --- Logs de LLMClient ----------------------------------------------------------------


class _FakeResponse:
    """Réponse requests minimale : statut OK + payload Ollama."""

    def raise_for_status(self):
        return None

    def json(self):
        return {"message": {"content": "réponse simulée"}}


def test_llm_client_logs_request_and_response(caplog, monkeypatch):
    """Chaque appel LLM trace le départ (INFO) puis la réponse avec sa durée."""
    monkeypatch.setattr(
        "agent.llm_client.requests.post", lambda url, **kwargs: _FakeResponse()
    )

    client = LLMClient("http://127.0.0.1:9/api/chat", "fake-model", timeout=5)
    with caplog.at_level(logging.INFO, logger="agent"):
        content = client.call([{"role": "user", "content": "salut"}])

    assert content == "réponse simulée"
    messages = [r.getMessage() for r in caplog.records if r.name == "agent.llm_client"]
    assert any("Appel LLM" in m and "fake-model" in m for m in messages), messages
    assert any("Réponse du LLM" in m and "reçue" in m for m in messages), messages


def test_llm_client_logs_timeout_as_error(caplog, monkeypatch):
    """Un timeout LLM est tracé en ERROR puis relancé à l'appelant."""

    def fake_post(url, **kwargs):
        raise requests.exceptions.Timeout("trop lent")

    monkeypatch.setattr("agent.llm_client.requests.post", fake_post)

    client = LLMClient("http://127.0.0.1:9/api/chat", "fake-model", timeout=1)
    with caplog.at_level(logging.INFO, logger="agent"):
        with pytest.raises(requests.exceptions.Timeout):
            client.call([{"role": "user", "content": "salut"}])

    errors = [
        r.getMessage()
        for r in caplog.records
        if r.name == "agent.llm_client" and r.levelno == logging.ERROR
    ]
    assert errors and "timeout" in errors[0].lower(), errors