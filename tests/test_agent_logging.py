"""Tests des logs structurés de l'agent IA (logger « thinktuning.agent »).

L'agent journalise chaque étape sur le logger standard **thinktuning.agent**
(même convention que « thinktuning.api » pour l'API), niveau piloté par la
variable AGENT_LOG_LEVEL. On vérifie ici les événements clés et leur niveau,
à la manière de tests/test_api_metrics.py. Aucun appel réseau : le LLM est
remplacé par un ScriptedLLM et requests.post est simulé.

Lance avec : pytest tests/test_agent_logging.py -v
"""

import logging
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import pytest  # noqa: E402

from core import agent_cache  # noqa: E402  (point d'entrée historique, plus de hack sys.path)

AGENT_LOGGER = "thinktuning.agent"


class ScriptedLLM:
    """Remplace LLMClient : renvoie les réponses scriptées puis 'Terminé.'."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def call(self, messages):
        self.calls.append([dict(m) for m in messages])
        if self.replies:
            return self.replies.pop(0)
        return "Terminé."


def _records(caplog):
    return [(r.levelname, r.getMessage()) for r in caplog.records]


# --- AgentCore -----------------------------------------------------------------------

def test_agentcore_logs_rounds_tool_calls_and_completion(caplog):
    core = agent_cache.AgentCore(
        ScriptedLLM(
            [
                '{"tool": "add", "args": {"a": 12, "b": 30}}',
                "C'est fait : le résultat est 42.",
            ]
        )
    )

    with caplog.at_level(logging.DEBUG, logger=AGENT_LOGGER):
        answer = core.run("calcule 12 + 30")

    records = _records(caplog)
    assert answer.startswith("C'est fait")

    # Début et fin de run, avec métadonnées.
    assert any(lvl == "INFO" and msg.startswith("run_start") for lvl, msg in records)
    done = [msg for lvl, msg in records if lvl == "INFO" and msg.startswith("run_done")]
    assert done and "answer_chars=" in done[-1]

    # Un log par round de planification.
    assert any(
        lvl == "INFO" and msg.startswith("round ") and "start=1/" in msg
        for lvl, msg in records
    )

    # Appel d'outil (avec arguments) + résultat.
    tool_calls = [msg for lvl, msg in records if lvl == "INFO" and msg.startswith("tool_call")]
    assert any("name=add" in m and "'a': 12" in m for m in tool_calls)
    assert any(
        lvl == "INFO" and msg.startswith("tool_result") and "name=add" in msg
        for lvl, msg in records
    )

    # Payload brut du LLM uniquement en DEBUG.
    assert any(lvl == "DEBUG" and msg.startswith("llm_raw_response") for lvl, msg in records)


def test_agentcore_logs_tool_failure_with_traceback(caplog):
    # add([1], 2) lève un TypeError à l'intérieur du tool -> logger.exception.
    core = agent_cache.AgentCore(
        ScriptedLLM(['{"tool": "add", "args": {"a": [1], "b": 2}}', "corrigé."])
    )

    with caplog.at_level(logging.ERROR, logger=AGENT_LOGGER):
        answer = core.run("additionne")

    records = _records(caplog)
    errors = [msg for lvl, msg in records if lvl == "ERROR"]
    assert any(msg.startswith("tool_error") and "name=add" in msg for msg in errors)
    # Traceback complet attaché au log (logger.exception).
    assert any(r.exc_info for r in caplog.records)
    # L'erreur repart quand même au LLM pour auto-correction.
    assert "ERREUR pendant 'add'" in answer


def test_agentcore_logs_auto_correction_warning(caplog):
    core = agent_cache.AgentCore(ScriptedLLM(['{"tool": "division", "args": {}}']))

    with caplog.at_level(logging.INFO, logger=AGENT_LOGGER):
        core.run("divise")

    warnings = [msg for lvl, msg in _records(caplog) if lvl == "WARNING"]
    assert any(msg.startswith("auto_correction") and "Tool inconnu" in msg for msg in warnings)


# --- LLMClient -----------------------------------------------------------------------

def test_llm_client_logs_request_and_response(caplog, monkeypatch):
    from ia.agent.llm_client import LLMClient

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def iter_lines(self, decode_unicode=False):
            yield '{"message": {"content": "réponse du modèle"}, "done": true}'

        def close(self):
            pass

    def fake_post(url, json=None, timeout=None, stream=False):
        assert url == "http://ollama.test/api/chat"
        assert json["model"] == "llama3.1:8b"
        assert json["stream"] is True
        return FakeResp()

    monkeypatch.setattr("requests.post", fake_post)
    client = LLMClient("http://ollama.test/api/chat", "llama3.1:8b", timeout=5)

    with caplog.at_level(logging.DEBUG, logger=AGENT_LOGGER):
        out = client.call([{"role": "user", "content": "bonjour"}])

    assert out == "réponse du modèle"
    records = _records(caplog)
    assert any(
        lvl == "INFO"
        and msg.startswith("llm_request")
        and "url=http://ollama.test/api/chat" in msg
        and "messages=1" in msg
        for lvl, msg in records
    )
    assert any(
        lvl == "INFO" and msg.startswith("llm_response") and "elapsed_ms=" in msg
        for lvl, msg in records
    )
    assert any(
        lvl == "DEBUG" and msg.startswith("llm_response_content") for lvl, msg in records
    )


def test_llm_client_parses_bytes_lines_when_no_charset(monkeypatch):
    """Correction : Ollama renvoie `Content-Type: application/x-ndjson` sans
    charset, donc `iter_lines(decode_unicode=True)` fournit des `bytes`.
    L'appel ne doit plus lever
    `TypeError: startswith first arg must be bytes or a tuple of bytes, not str`.
    """
    from ia.agent.llm_client import LLMClient

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def iter_lines(self, decode_unicode=False):
            # Simule le comportement réel de requests quand l'encodage est
            # indéterminé : les lignes sont émises en tant que `bytes` UTF-8.
            yield ('{"message": {"content": "réponse de bytes"}, "done": true}').encode()

        def close(self):
            pass

    def fake_post(url, json=None, timeout=None, stream=False):
        return FakeResp()

    monkeypatch.setattr("requests.post", fake_post)
    client = LLMClient("http://ollama.test/api/chat", "llama3.1:8b", timeout=5)
    assert client.call([{"role": "user", "content": "bonjour"}]) == "réponse de bytes"


def test_llm_client_repairs_latin1_mojibake_str_lines(monkeypatch):
    """Double encodage : si un provider/proxy renvoie déjà le flux décodé en
    Latin-1 (chaîne ``str`` mojibake), ``call()`` doit réparer le contenu
    final. Avant la correction, ce texte sortait cassé (« tÃªte ») et partait
    tel quel au dashboard ET en base.
    """
    from ia.agent.llm_client import LLMClient

    # Contenu français correct, puis son mojibake Latin-1 (comme si requests
    # avait choisi iso-8859-1) : « Bonjour, tête désolée. À l'aide ! »
    expected = "Bonjour, tête désolée. À l'aide !"
    mojibake = expected.encode("utf-8").decode("latin-1")

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def iter_lines(self, decode_unicode=False):
            yield '{"message": {"content": "%s"}, "done": true}' % mojibake

        def close(self):
            pass

    monkeypatch.setattr("requests.post", lambda url, json=None, timeout=None, stream=False: FakeResp())
    client = LLMClient("http://ollama.test/api/chat", "llama3.1:8b", timeout=5)
    assert client.call([{"role": "user", "content": "bonjour"}]) == expected
    assert client.last_thinking == ""


def test_llm_client_logs_timeout_as_error(caplog, monkeypatch):
    from ia.agent import llm_client as llm_module

    def slow_post(url, json=None, timeout=None, stream=False):
        raise llm_module.requests.exceptions.Timeout("too slow")

    monkeypatch.setattr(llm_module.requests, "post", slow_post)
    client = llm_module.LLMClient("http://127.0.0.1:9/api/chat", "m", timeout=1)

    with caplog.at_level(logging.ERROR, logger=AGENT_LOGGER):
        with pytest.raises(llm_module.requests.exceptions.Timeout):
            client.call([])

    assert any(
        r.levelname == "ERROR" and r.getMessage().startswith("llm_timeout")
        for r in caplog.records
    )
