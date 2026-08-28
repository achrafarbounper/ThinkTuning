"""Tests Phase B — découverte/recommandation d'outils, analytique, plugins.

Hors-ligne : aucun appel réseau ni LLM. Les tests API réutilisent les fixtures
``client`` / ``HEADERS`` de conftest.py (même conventions que test_agent_audit).
"""

import os
import sys
import types

_IA_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ia")
if _IA_ROOT not in sys.path:
    sys.path.insert(0, _IA_ROOT)

import pytest

import tools.plugin as plugin_mod
import tools.tool_analytics as analytics
from tools.tool_discovery import suggest_tools


# ---------------------------------------------------------------------------
# Découverte / recommandation
# ---------------------------------------------------------------------------

def test_suggest_tools_finds_calc_for_expression():
    out = suggest_tools("évalue l'expression 2+3*4", k=5)
    assert out, "au moins une suggestion attendue"
    assert out[0]["tool"] == "calc"
    assert 0 < out[0]["score"] <= 1.0
    assert out[0]["reasons"]


def test_suggest_tools_exact_name_mention_wins():
    out = suggest_tools("utilise read_file sur notes.txt", k=3)
    assert out[0]["tool"] == "read_file"


def test_suggest_tools_empty_or_noise_returns_empty():
    assert suggest_tools("") == []
    assert suggest_tools("   ") == []
    # Aucun terme en rapport : pas de recommandation à vide.
    assert suggest_tools("xyzzy qwerty zzzz") == []


def test_suggest_tools_respects_k_and_is_sorted():
    out = suggest_tools("lire le contenu d'un fichier", k=2)
    assert len(out) <= 2
    scores = [s["score"] for s in out]
    assert scores == sorted(scores, reverse=True)


def test_suggest_tools_synonym_keyword():
    out = suggest_tools("quel temps fait-il, donne l'heure", k=3)
    assert any(s["tool"] == "now" for s in out)


# ---------------------------------------------------------------------------
# Analytique d'usage
# ---------------------------------------------------------------------------

def test_record_usage_and_stats_roundtrip():
    analytics.reset_usage()
    analytics.record_usage("add", 12.0)
    analytics.record_usage("add", 8.0)
    analytics.record_usage("add", 10.0, error=True)
    stats = analytics.get_stats()
    entry = stats["add"]
    assert entry["calls"] == 3
    assert entry["errors"] == 1
    assert entry["error_rate"] == round(1 / 3, 3)
    assert entry["avg_ms"] == round(30.0 / 3, 1)
    analytics.get_stats(reset=True)
    assert analytics.get_stats().get("add") is None


def test_record_call_context_manager_times_and_counts():
    with analytics.record_call("now"):
        pass
    stats = analytics.get_stats()
    assert stats["now"]["calls"] == 1
    assert stats["now"]["errors"] == 0
    with pytest.raises(ValueError):
        with analytics.record_call("now"):
            raise ValueError("boom")
    stats = analytics.get_stats()
    assert stats["now"]["calls"] == 2
    assert stats["now"]["errors"] == 1


# ---------------------------------------------------------------------------
# Plugins
# ---------------------------------------------------------------------------

@pytest.fixture()
def fake_plugin():
    """Injecte un module plugin fictif dans sys.modules et nettoie après."""
    module = types.ModuleType("fake_tt_plugin")
    module.PLUGIN_NAME = "FakeTT"
    module.TOOL_META = {
        "fake_echo": {
            "description": "renvoie le texte passé",
            "required_args": ["text"],
            "parameters": {"text": {"type": "string"}},
        },
    }

    def fake_echo(text: str) -> str:
        return f"echo:{text}"

    module.fake_echo = fake_echo
    sys.modules["fake_tt_plugin"] = module
    yield module
    for name in ("fake_echo",):
        plugin_mod.TOOLS.pop(name, None)
        plugin_mod.TOOL_META.pop(name, None)
        plugin_mod.REQUIRED_ARGS.pop(name, None)
    sys.modules.pop("fake_tt_plugin", None)
    plugin_mod._LOADED.pop("fake_tt_plugin", None)


def test_plugin_load_registers_tool(fake_plugin):
    out = plugin_mod.load_plugin("fake_tt_plugin")
    assert out["plugin"] == "FakeTT"
    assert out["registered"] == ["fake_echo"]
    from tools.tool_registry import REQUIRED_ARGS, TOOLS
    assert "fake_echo" in TOOLS
    assert REQUIRED_ARGS["fake_echo"] == ["text"]
    assert TOOLS["fake_echo"](text="hi") == "echo:hi"


def test_plugin_load_idempotent(fake_plugin):
    first = plugin_mod.load_plugin("fake_tt_plugin")
    second = plugin_mod.load_plugin("fake_tt_plugin")
    assert first["cached"] is False
    assert second["cached"] is True
    assert second["registered"] == []


def test_plugin_rejects_conflicting_tool_name(fake_plugin):
    plugin_mod.load_plugin("fake_tt_plugin")
    dup = types.ModuleType("fake_tt_dup")
    dup.TOOL_META = {"add": {"description": "dup", "required_args": []}}
    dup.add = lambda a, b: 0
    sys.modules["fake_tt_dup"] = dup
    try:
        with pytest.raises(plugin_mod.PluginError):
            plugin_mod.load_plugin("fake_tt_dup")
    finally:
        sys.modules.pop("fake_tt_dup", None)
    from tools.tool_registry import TOOLS
    assert "fake_echo" in TOOLS  # le plugin initial est intact


def test_plugin_rejects_tool_without_meta(fake_plugin):
    mod = types.ModuleType("fake_tt_nometa")
    mod.TOOL_META = {}

    def rogue():
        return 1

    mod.rogue = rogue
    sys.modules["fake_tt_nometa"] = mod
    try:
        with pytest.raises(plugin_mod.PluginError, match="TOOL_META"):
            plugin_mod.load_plugin("fake_tt_nometa")
    finally:
        sys.modules.pop("fake_tt_nometa", None)
        plugin_mod._LOADED.pop("fake_tt_nometa", None)
        plugin_mod.TOOLS.pop("rogue", None)


# ---------------------------------------------------------------------------
# Endpoints API (gated par AGENT_TOOL_ANALYTICS)
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

os.environ.setdefault("API_KEY", "test-key")  # avant l'import de l'app

from api import app as api_app  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture()
def client():
    with TestClient(api_app) as test_client:
        yield test_client


def test_recommend_and_stats_require_flag(client, monkeypatch):
    monkeypatch.delenv("AGENT_TOOL_ANALYTICS", raising=False)
    assert client.get("/api/agent/tools/recommend?q=add", headers=HEADERS).status_code == 404
    assert client.get("/api/agent/tools/stats", headers=HEADERS).status_code == 404


def test_recommend_endpoint(client, monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_ANALYTICS", "on")
    resp = client.get(
        "/api/agent/tools/recommend",
        params={"q": "calcule 2+2", "k": 3},
        headers=HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["query"] == "calcule 2+2"
    assert body["suggestions"] and "tool" in body["suggestions"][0]


def test_stats_endpoint_reports_usage(client, monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_ANALYTICS", "on")
    run = client.post(
        "/api/agent/tools/run",
        json={"tool": "add", "args": {"a": 1, "b": 2}},
        headers=HEADERS,
    )
    assert run.status_code == 200
    resp = client.get("/api/agent/tools/stats", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["tools"].get("add", {}).get("calls", 0) >= 1
    assert "plugins" in body
