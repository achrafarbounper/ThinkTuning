"""Tests offline de la fenêtre de contexte par défaut (2048) de l'agent IA.

Couvre :
    - LLMClient : valeur par défaut (2048) et transmission `options.num_ctx`
      au payload envoyé à Ollama, y compris l'override via `context_length` ;
    - core.agent_cache.agent_config : défaut 2048 et surcharge via env
      AGENT_CONTEXT_LENGTH ;
    - core.agent_cache._build_runner : le contexte est bien transmis au LLMClient.

Aucun appel réseau : `requests.post` est simulé (du côté LLMClient) et le
runner est construit sans LLM réel. Comment lancer :
    pytest tests/test_agent_context_length.py -v
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IA_DIR = os.path.join(PROJECT_ROOT, "ia")
for _p in (PROJECT_ROOT, IA_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)


from core import agent_cache  # noqa: E402  (insère ia/ dans sys.path)


class FakeResp:
    status_code = 200

    def raise_for_status(self):
        pass

    def iter_lines(self, decode_unicode=False):
        yield '{"message": {"content": "ok"}, "done": true}'

    def close(self):
        pass


# --- LLMClient -------------------------------------------------------------


def test_llm_client_default_context_is_2048_and_sent_to_ollama(monkeypatch):
    from ia.agent.llm_client import LLMClient

    captured = {}

    def fake_post(url, json=None, timeout=None, stream=False):
        captured["json"] = json
        return FakeResp()

    monkeypatch.setattr("requests.post", fake_post)
    client = LLMClient("http://ollama.test/api/chat", "llama3.1:8b", timeout=5)

    assert client.context_length == 2048
    client.call([{"role": "user", "content": "bonjour"}])

    options = captured["json"]["options"]
    assert options["num_ctx"] == 2048


def test_llm_client_sends_custom_context_length(monkeypatch):
    from ia.agent.llm_client import LLMClient

    captured = {}

    def fake_post(url, json=None, timeout=None, stream=False):
        captured["json"] = json
        return FakeResp()

    monkeypatch.setattr("requests.post", fake_post)
    client = LLMClient(
        "http://ollama.test/api/chat", "llama3.1:8b", timeout=5, context_length=4096
    )

    assert client.context_length == 4096
    client.call([{"role": "user", "content": "bonjour"}])
    assert captured["json"]["options"]["num_ctx"] == 4096


# --- core.agent_cache ------------------------------------------------------------


def test_agent_config_context_length_defaults_to_2048(monkeypatch):
    monkeypatch.delenv("AGENT_CONTEXT_LENGTH", raising=False)
    assert agent_cache.agent_config()["context_length"] == 2048


def test_agent_config_context_length_reads_env(monkeypatch):
    monkeypatch.setenv("AGENT_CONTEXT_LENGTH", "8192")
    assert agent_cache.agent_config()["context_length"] == 8192


def test_build_runner_forwards_context_length(monkeypatch):
    captured = {}
    monkeypatch.setenv("AGENT_CONTEXT_LENGTH", "4096")

    def fake_llm_init(
        self,
        url,
        model,
        timeout=None,
        temperature=None,
        think=False,
        context_length=None,
        # Nouveaux paramètres multi-provider (provider/api_key) : acceptés
        # pour rester compatible avec la fabrique sans altérer les assertions.
        **kwargs,
    ):
        captured["url"] = url
        captured["model"] = model
        captured["context_length"] = context_length

    # On patche la classe utilisée par agent_cache (`agent.llm_client`), pas
    # celle du paquet `ia.agent` qui est un autre objet module.
    monkeypatch.setattr(agent_cache.LLMClient, "__init__", fake_llm_init)

    # Le runner est construit avec la config env ; on évite d'exécuter un run.
    runner = agent_cache._build_runner()
    assert runner is not None
    assert captured["context_length"] == 4096
