"""
Tests offline du noyau de l'agent IA et de son intégration API.

Historique : ce fichier couvrait le serveur autonome `ia/api_server.py`,
supprimé au profit des routes `/api/agent/*` du package `api` (couvertes par
tests/test_api_ai_chat.py). On teste ici :
    - AgentCore directement : boucle multi-round, auto-correction, budget ;
    - core.agent_cache.ask_agent : traduction des erreurs réseau LLM en HTTP
      (Timeout -> 504, ConnectionError/HTTPError -> 502).

Complète tests/test_api_ai_chat.py (cas basiques) avec l'auto-correction de
l'agent et la traduction des erreurs réseau en codes HTTP. Aucun appel
réseau : le LLM (Ollama) est remplacé par un FakeLLM scripté injecté dans
le cache `core.agent_cache`.
Lance avec : pytest tests/test_agent_api.py -v
"""

import os

# Config test AVANT tout import (le cache insère ia/ dans sys.path).
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("AGENT_OLLAMA_URL", "http://127.0.0.1:9/api/chat")  # port factice

import pytest  # noqa: E402
import requests  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from core import agent_cache  # noqa: E402  (insère ia/ dans sys.path)

AgentCore = agent_cache.AgentCore
AgentRunner = agent_cache.AgentRunner
ALL_TOOLS = agent_cache.TOOLS


class FakeLLM:
    """Remplace LLMClient.

    Réponses scriptées via `replies` (dépilées une par une), ou exception
    réseau simulée via `error` (Timeout, ConnectionError...).
    Sans réponse scriptée disponible, un comportement par défaut raisonnable
    est simulé : planification de l'outil `add` sur un prompt d'addition,
    puis conclusion en TEXTE NORMAL dès qu'un résultat d'outil est présent.
    `responses` conserve toutes les réponses effectivement renvoyées.
    """

    def __init__(self):
        self.calls: list[list[dict]] = []
        self.replies: list[str] = []
        self.returned: list[str] = []
        self.error: Exception | None = None

    @property
    def responses(self) -> list[str]:
        return self.returned

    def call(self, messages):
        self.calls.append([dict(m) for m in messages])
        if self.error is not None:
            raise self.error
        reply = self.replies.pop(0) if self.replies else self._default_reply(messages)
        self.returned.append(reply)
        return reply

    @staticmethod
    def _last_user_content(messages) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return message.get("content", "")
        return ""

    def _default_reply(self, messages) -> str:
        """Script par défaut : `add` sur demande d'addition, puis conclusion."""
        last_user = self._last_user_content(messages)
        if last_user.startswith("Dernier résultat") or last_user.startswith(
            "Nombre maximum d'étapes"
        ):
            # Un outil a tourné (ou budget épuisé) : conclure SANS JSON.
            return "J'ai additionné les nombres et voici le résultat obtenu."
        if "Arguments manquants" in last_user and "add" in last_user.lower():
            # Auto-correction attendue : renvoyer le même outil avec des args.
            return '{"tool": "add", "args": {"a": 12, "b": 30}}'
        lowered = last_user.lower()
        if "additionn" in lowered or "+" in last_user:
            return '{"tool": "add", "args": {"a": 12, "b": 30}}'
        return "Je n'ai pas compris la demande."  # texte simple, jamais de JSON


# --- Registre ---------------------------------------------------------------------

def test_registry_exposes_expected_tools():
    assert {"add", "write_file", "gpu_info", "docker_exec"} <= set(ALL_TOOLS)
    # Cohérence du registre central partagé entre l'API et AgentCore.
    assert set(ALL_TOOLS) == set(agent_cache.REQUIRED_ARGS)


# --- AgentCore : boucle principale -------------------------------------------------

def test_agentcore_runs_tool_then_explains():
    llm = FakeLLM()
    answer = AgentRunner(AgentCore(llm)).ask("Additionne 12 + 30 avec l'outil add.")

    assert "additionné" in answer
    # Deux appels LLM : 1) planification JSON, 2) explication finale.
    assert len(llm.calls) == 2
    assert llm.responses[0].startswith('{"tool": "add"')
    assert llm.calls[1][-1]["content"].startswith("Dernier résultat")
    assert "42" in llm.calls[1][-1]["content"]


def test_agentcore_returns_direct_text_answer_when_no_tool_needed():
    """Réponse texte sans JSON au 1er tour = réponse valide renvoyée telle quelle.

    Comportement volontairement modifié (ex-« Réponse non exploitable ») :
    la liste des outils étant désormais injectée dans le prompt système, une
    réponse directe en texte est légitime (salutation, explication…) et ne
    doit plus être écrasée par un préfixe d'erreur.
    """
    llm = FakeLLM()
    llm.replies = ["Bonjour ! Comment puis-je vous aider ?"]
    answer = AgentCore(llm).run("salut")

    assert answer == "Bonjour ! Comment puis-je vous aider ?"


def test_agentcore_auto_corrects_unknown_tool():
    """Tool inconnu -> message d'erreur renvoyé au LLM qui se corrige."""
    llm = FakeLLM()
    llm.replies = ['{"tool": "division", "args": {"a": 1}}']
    answer = AgentCore(llm).run("divise 1 par 2")

    assert "[auto-correction]" in answer
    assert "Tool inconnu" in answer
    corrective_prompts = [
        c[-1]["content"] for c in llm.calls if c[-1]["content"].startswith("Tool inconnu")
    ]
    assert corrective_prompts


def test_agentcore_auto_corrects_missing_args():
    llm = FakeLLM()
    llm.replies = ['{"tool": "add", "args": {}}']
    answer = AgentCore(llm).run("additionne")

    # Les arguments manquants sont listés puis l'agent se corrige (add -> 42).
    assert "'a'" in answer and "'b'" in answer
    assert "[auto-correction]" in answer


def test_agentcore_stops_at_max_rounds_budget():
    llm = FakeLLM()
    # Une réponse invalide par tour : le LLM ne se corrige jamais.
    llm.replies = ['{"tool": "division"}'] * 3
    answer = AgentCore(llm, max_rounds=3).run("divise")

    # Budget respecté : 3 tours de correction + 1 appel de conclusion.
    assert len(llm.calls) == 4
    assert "[auto-correction]" in answer
    assert "Tool inconnu" in answer


# --- core.agent_cache.ask_agent : intégration API -----------------------------------

def _inject_runner(monkeypatch, llm):
    """Remplace le runner mis en cache par un runner adossé au FakeLLM."""
    monkeypatch.setattr(
        agent_cache, "_runner", agent_cache.AgentRunner(agent_cache.AgentCore(llm))
    )


def test_ask_agent_returns_final_answer(monkeypatch):
    _inject_runner(monkeypatch, FakeLLM())

    assert "additionné" in agent_cache.ask_agent("Additionne 12 + 30.")


def test_ask_agent_maps_timeout_to_504(monkeypatch):
    llm = FakeLLM()
    llm.error = requests.exceptions.Timeout("too slow")
    _inject_runner(monkeypatch, llm)

    with pytest.raises(HTTPException) as excinfo:
        agent_cache.ask_agent("salut")
    assert excinfo.value.status_code == 504


def test_ask_agent_maps_connection_error_to_502(monkeypatch):
    llm = FakeLLM()
    llm.error = requests.exceptions.ConnectionError("connection refused")
    _inject_runner(monkeypatch, llm)

    with pytest.raises(HTTPException) as excinfo:
        agent_cache.ask_agent("salut")
    assert excinfo.value.status_code == 502
    assert "injoignable" in excinfo.value.detail


def test_ask_agent_maps_http_error_to_502(monkeypatch):
    llm = FakeLLM()
    response = requests.Response()
    response.status_code = 500
    llm.error = requests.exceptions.HTTPError("boom", response=response)
    _inject_runner(monkeypatch, llm)

    with pytest.raises(HTTPException) as excinfo:
        agent_cache.ask_agent("salut")
    assert excinfo.value.status_code == 502
