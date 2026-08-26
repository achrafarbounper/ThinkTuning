"""
Tests offline du mode « Réflexion » (thinking) de l'agent IA.

Couvre :
    - ia/agent/thinking.extract_thinking : séparation des balises <think> ;
    - AgentCore.run_detailed : collecte de la réflexion multi-rounds,
      run() conservant son comportement historique ;
    - system_prompt.THINKING_PROMPT_SECTION : injection conditionnelle du
      mode « Réflexion » dans le prompt système ;
    - LLMClient : paramètre « think » d'Ollama, champ natif message.thinking,
      repli sur les balises <think> inline ;
    - POST /api/ai : événements SSE thinking_delta diffusés AVANT les delta,
      contrat historique inchangé quand le mode est désactivé ;
    - core.agent_cache : ask_agent_detailed + cache des runners « réflexion ».

Aucun appel réseau : le LLM (Ollama) est remplacé par un FakeLLM scripté.
Lance avec : pytest tests/test_agent_thinking.py -v
"""

import json
import os

# Config test AVANT tout import (le cache insère ia/ dans sys.path).
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("AGENT_OLLAMA_URL", "http://127.0.0.1:9/api/chat")  # port factice

from fastapi.testclient import TestClient  # noqa: E402

# ORDRE IMPORTANT : importer agent_cache AVANT tout module « agent.* »,
# c'est lui qui ajoute le dossier ia/ au sys.path.
from core import agent_cache  # noqa: E402
from agent.thinking import extract_thinking  # noqa: E402
from api import app  # noqa: E402

AgentCore = agent_cache.AgentCore
AgentRunner = agent_cache.AgentRunner
LLMClient = agent_cache.LLMClient

HEADERS = {"X-API-Key": "test-key"}
client = TestClient(app)


class FakeLLM:
    """Remplace LLMClient ; réponses scriptées via `replies`.

    `native_thinking` simule le champ Ollama « message.thinking » : liste
    dépilée à chaque appel, ou chaîne constante ; exposé sur last_thinking
    exactement comme le fait le vrai client.
    """

    def __init__(self):
        self.calls: list[list[dict]] = []
        self.replies: list[str] = []
        self.returned: list[str] = []
        self.native_thinking: list[str] | str = []
        self.last_thinking = ""

    def call(self, messages):
        self.calls.append([dict(m) for m in messages])
        if isinstance(self.native_thinking, list):
            self.last_thinking = self.native_thinking.pop(0) if self.native_thinking else ""
        else:
            self.last_thinking = self.native_thinking
        reply = self.replies.pop(0) if self.replies else "Réponse par défaut."
        self.returned.append(reply)
        return reply


def _patch_build_runner(monkeypatch, llm):
    """Remplace la fabrique de runners par une version adossée au FakeLLM.

    Couvre les deux chemins de get_agent_runner : historique (_build_runner())
    et « Réflexion » (_build_runner(..., enable_thinking=True)). Le runner
    partagé est réinitialisé pour éviter toute fuite d'état entre tests.
    """

    def fake_build(model_name=None, enable_thinking=False):
        return AgentRunner(AgentCore(llm, enable_thinking=enable_thinking))

    monkeypatch.setattr(agent_cache, "_build_runner", fake_build)
    monkeypatch.setattr(agent_cache, "_runner", None)
    # Les runners « réflexion » sont mis en cache sous "<modèle>::thinking" :
    # sans purge, une requête active le runner d'un TEST PRÉCÉDENT (dont le
    # FakeLLM est épuisé) au lieu de celui-ci.
    monkeypatch.setattr(agent_cache, "_override_runners", {})


# --- ia/agent/thinking.extract_thinking ----------------------------------------------

def test_extract_thinking_without_blocks_returns_text_unchanged():
    cleaned, thinking = extract_thinking('{"tool": "add", "args": {"a": 12, "b": 30}}')
    assert cleaned == '{"tool": "add", "args": {"a": 12, "b": 30}}'
    assert thinking == ""


def test_extract_thinking_separates_single_block():
    raw = '<think>Il faut additionner.</think>{"tool": "add"}'
    cleaned, thinking = extract_thinking(raw)
    assert cleaned == '{"tool": "add"}'
    assert thinking == "Il faut additionner."


def test_extract_thinking_joins_multiple_blocks():
    raw = "<think>Première étape</think>milieu<think>Deuxième étape</think>fin"
    cleaned, thinking = extract_thinking(raw)
    assert cleaned == "milieufin"
    assert thinking == "Première étape\n\nDeuxième étape"


def test_extract_thinking_tolerates_unclosed_tag():
    """Génération tronquée en pleine réflexion : la suite est la réflexion."""
    cleaned, thinking = extract_thinking("<think>Raisonnement interrompu")
    assert cleaned == ""
    assert thinking == "Raisonnement interrompu"


def test_extract_thinking_tolerates_case_and_spaces():
    raw = "< THINK >Réfléchir< / think >Agir"
    cleaned, thinking = extract_thinking(raw)
    assert cleaned == "Agir"
    assert thinking == "Réfléchir"


def test_extract_thinking_removes_orphan_closing_tag():
    cleaned, thinking = extract_thinking("</think>Réponse seule")
    assert cleaned == "Réponse seule"
    assert thinking == ""


# --- AgentCore : run() historique vs run_detailed() ----------------------------------

def test_agentcore_run_strips_think_tags_and_keeps_tools_flow():
    """run() historique : les balises <think> sont retirées, le tool tourne."""
    llm = FakeLLM()
    llm.replies = [
        '<think>L\'utilisateur veut 12+30.</think>{"tool": "add", "args": {"a": 12, "b": 30}}',
        "J'ai additionné les nombres.",
    ]
    answer = AgentCore(llm).run("Additionne 12 + 30.")

    assert "additionné" in answer
    assert "<think>" not in answer
    # Deux appels LLM : planification JSON puis explication finale.
    assert len(llm.calls) == 2
    assert llm.calls[1][-1]["content"].startswith("Dernier résultat")
    assert "42" in llm.calls[1][-1]["content"]


def test_run_detailed_collects_thinking_from_both_rounds():
    llm = FakeLLM()
    llm.replies = [
        '<think>Je planifie l\'addition.</think>{"tool": "add", "args": {"a": 12, "b": 30}}',
        '<think>Vérification : 42 attendu.</think>J\'ai additionné les nombres.',
    ]
    result = AgentCore(llm, enable_thinking=True).run_detailed("Additionne 12 + 30.")

    assert "additionné" in result.answer
    assert "<think>" not in result.answer
    assert "Je planifie" in result.thinking
    assert "Vérification" in result.thinking


def test_run_detailed_collects_native_ollama_thinking():
    """Le champ natif message.thinking (llm.last_thinking) est aussi collecté."""
    llm = FakeLLM()
    llm.replies = ['{"tool": "add", "args": {"a": 12, "b": 30}}', "Terminé."]
    llm.native_thinking = ["Raisonnement natif Ollama."]
    result = AgentCore(llm, enable_thinking=True).run_detailed("Additionne 12 + 30.")

    assert "Raisonnement natif Ollama." in result.thinking
    assert result.answer.strip() == "Terminé."


def test_system_prompt_includes_thinking_rules_only_when_enabled():
    prompt_off = AgentCore(FakeLLM()).system_prompt
    prompt_on = AgentCore(FakeLLM(), enable_thinking=True).system_prompt

    assert "<think>" not in prompt_off
    assert "MODE RÉFLEXION" in prompt_on
    # La section s'ajoute au prompt standard, elle ne le remplace pas.
    assert prompt_on.startswith(prompt_off)


def test_run_and_run_detailed_share_the_same_answer():
    replies = [
        '{"tool": "add", "args": {"a": 12, "b": 30}}',
        "Voilà le résultat.",
    ]
    llm_a, llm_b = FakeLLM(), FakeLLM()
    llm_a.replies, llm_b.replies = list(replies), list(replies)

    answer_a = AgentCore(llm_a).run("calcule")
    answer_b = AgentCore(llm_b).run_detailed("calcule").answer
    assert answer_a == answer_b


def test_run_detailed_forwards_thinking_via_callback():
    """La trace de réflexion est émise au fil de l'eau via on_thinking.

    Client sans `call_stream` (FakeLLM) : les blocs inline et natifs sont
    transmis par le callback dès la fin de chaque appel LLM — le contrat
    temps réel de l'API (thinking_delta) repose sur ce branchement.
    """
    llm = FakeLLM()
    llm.replies = [
        '<think>Je planifie.</think>{"tool": "add", "args": {"a": 12, "b": 30}}',
        "J'ai additionné les nombres.",
    ]
    forwarded: list[str] = []
    result = AgentCore(llm, enable_thinking=True).run_detailed(
        "Additionne 12 + 30.", on_thinking=forwarded.append
    )

    assert forwarded, "la réflexion doit être émise via le callback"
    assert "Je planifie" in forwarded[0]
    assert "Je planifie" in result.thinking
    assert "additionné" in result.answer


# --- LLMClient : paramètre think + champ natif Ollama --------------------------------

class _FakeStreamResponse:
    """Réponse requests STREAMING minimale pour monkeypatcher requests.post.

    ``lines`` contient le flux NDJSON simulé d'Ollama (une chaîne par ligne) ;
    l'itérateur reproduit ``resp.iter_lines(decode_unicode=True)``.
    """

    def __init__(self, lines):
        self._lines = list(lines)
        self.status_code = 200

    def raise_for_status(self):
        pass

    def iter_lines(self, decode_unicode=False):
        for line in self._lines:
            yield line

    def close(self):
        pass


def test_llm_client_sends_think_flag_when_enabled(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None, stream=False):
        captured["payload"] = json
        return _FakeStreamResponse(
            [
                '{"message": {"role": "assistant", "thinking": "Étape 1. ", "content": "La répon"}}',
                '{"message": {"role": "assistant", "thinking": "Étape 2. ", "content": "se."}, "done": true}',
            ]
        )

    import agent.llm_client as llm_module

    monkeypatch.setattr(llm_module.requests, "post", fake_post)

    ollama = LLMClient("http://ollama/api/chat", "deepseek-r1:8b", timeout=5, think=True)
    content = ollama.call([{"role": "user", "content": "question"}])

    assert content == "La réponse."
    assert ollama.last_thinking == "Étape 1. Étape 2."
    assert captured["payload"]["think"] is True
    assert captured["payload"]["stream"] is True


def test_llm_client_streams_thinking_and_content_in_real_time(monkeypatch):
    """call_stream émet chaque fragment via les callbacks, dans l'ordre du flux."""
    thinking_chunks: list[str] = []
    content_chunks: list[str] = []

    def fake_post(url, json=None, timeout=None, stream=False):
        return _FakeStreamResponse(
            [
                '{"message": {"role": "assistant", "thinking": "Premier "}}',
                '{"message": {"role": "assistant", "thinking": "jet ", "content": "Bon"}}',
                '{"message": {"role": "assistant", "content": "jour"}, "done": true}',
            ]
        )

    import agent.llm_client as llm_module

    monkeypatch.setattr(llm_module.requests, "post", fake_post)

    ollama = LLMClient("http://ollama/api/chat", "deepseek-r1:8b", think=True)
    content = ollama.call_stream(
        [], on_thinking=thinking_chunks.append, on_content=content_chunks.append
    )

    assert content == "Bonjour"
    assert thinking_chunks == ["Premier ", "jet "]
    assert content_chunks == ["Bon", "jour"]
    # Dans le flux Ollama, la réflexion arrive TOUJOURS avant le contenu.
    assert "".join(thinking_chunks).startswith("Premier")


def test_llm_client_omits_think_flag_by_default(monkeypatch):
    """Sans think=True le paramètre n'est PAS envoyé (serveurs/modèles anciens)."""
    captured = {}

    def fake_post(url, json=None, timeout=None, stream=False):
        captured["payload"] = json
        return _FakeStreamResponse(
            ['{"message": {"role": "assistant", "content": "OK"}, "done": true}']
        )

    import agent.llm_client as llm_module

    monkeypatch.setattr(llm_module.requests, "post", fake_post)

    ollama = LLMClient("http://ollama/api/chat", "llama3.1:8b")
    assert ollama.call([{"role": "user", "content": "salut"}]) == "OK"
    assert captured["payload"]["stream"] is True
    assert "think" not in captured["payload"]
    assert ollama.last_thinking == ""


def test_llm_client_falls_back_to_inline_think_tags(monkeypatch):
    """Anciens serveurs : balises <think> inline extraites du contenu."""

    def fake_post(url, json=None, timeout=None, stream=False):
        return _FakeStreamResponse(
            [
                '{"message": {"role": "assistant", "content": "<think>Raisonnement inline.</think>Réponse propre."}, "done": true}'
            ]
        )

    import agent.llm_client as llm_module

    monkeypatch.setattr(llm_module.requests, "post", fake_post)

    ollama = LLMClient("http://ollama/api/chat", "deepseek-r1:8b")
    assert ollama.call([]) == "Réponse propre."
    assert ollama.last_thinking == "Raisonnement inline."


# --- core.agent_cache : ask_agent_detailed + cache des runners ------------------------

def test_get_agent_runner_caches_one_thinking_runner(monkeypatch):
    calls = []

    def fake_build(model_name=None, enable_thinking=False):
        calls.append((model_name, enable_thinking))
        return object()

    monkeypatch.setattr(agent_cache, "_build_runner", fake_build)
    monkeypatch.setattr(agent_cache, "_override_runners", {})

    first = agent_cache.get_agent_runner(enable_thinking=True)
    second = agent_cache.get_agent_runner(enable_thinking=True)

    assert first is second  # un seul runner « réflexion » construit
    assert calls == [(None, True)]


def test_ask_agent_detailed_returns_answer_and_thinking(monkeypatch):
    llm = FakeLLM()
    llm.replies = [
        '{"tool": "add", "args": {"a": 12, "b": 30}}',
        '<think>Vérifié : 42.</think>J\'ai additionné les deux nombres.',
    ]
    _patch_build_runner(monkeypatch, llm)

    detailed = agent_cache.ask_agent_detailed("Additionne.", enable_thinking=True)
    assert "additionné" in detailed["answer"]
    assert "Vérifié : 42." in detailed["thinking"]

    plain = agent_cache.ask_agent("Additionne.")
    assert isinstance(plain, str) and plain


# --- POST /api/ai : contrat SSE avec réflexion -----------------------------------------

def test_ai_streams_thinking_delta_before_answer_delta(monkeypatch):
    llm = FakeLLM()
    llm.replies = [
        '<think>J\'additionne 12 et 30.</think>{"tool": "add", "args": {"a": 12, "b": 30}}',
        '<think>Résultat vérifié.</think>J\'ai additionné les nombres.',
    ]
    _patch_build_runner(monkeypatch, llm)

    resp = client.post(
        "/api/ai",
        json={"message": "Additionne 12 + 30.", "enable_thinking": True},
        headers=HEADERS,
    )

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "data: [DONE]" in resp.text

    # Réassemblage des événements SSE : le flux est fragmenté mot à mot, on
    # ne peut donc pas chercher des phrases continues dans resp.text.
    events = [
        json.loads(line[len("data: "):])
        for line in resp.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    thinking = "".join(event.get("thinking_delta", "") for event in events)
    answer = "".join(event.get("delta", "") for event in events)

    # La réflexion des DEUX rounds est diffusée, puis la réponse finale.
    assert thinking.startswith("J'additionne 12 et 30.")
    assert "Résultat vérifié" in thinking
    assert answer.strip() == "J'ai additionné les nombres."

    # La réflexion arrive EN PREMIER : chaque événement thinking_delta précède
    # le premier fragment de réponse.
    first_answer_index = next(i for i, ev in enumerate(events) if ev.get("delta"))
    thinking_indexes = [i for i, ev in enumerate(events) if ev.get("thinking_delta")]
    assert thinking_indexes and max(thinking_indexes) < first_answer_index


def test_ai_without_thinking_keeps_historical_contract(monkeypatch):
    """Sans enable_thinking : aucun événement thinking_delta, flux inchangé."""
    llm = FakeLLM()
    llm.replies = [
        '{"tool": "add", "args": {"a": 12, "b": 30}}',
        "Bonjour cher utilisateur.",
    ]
    _patch_build_runner(monkeypatch, llm)

    resp = client.post("/api/ai", json={"message": "salut"}, headers=HEADERS)

    assert resp.status_code == 200, resp.text
    assert "thinking_delta" not in resp.text
    assert 'data: {"delta": "Bonjour ' in resp.text
    assert "data: [DONE]" in resp.text