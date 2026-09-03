"""Câblage SSE sur ``EventBusPort`` — le noyau publie, la route s'abonne.

Prouve, avec des fakes déterministes (zéro réseau, zéro SQLite), que :

1. ``AgentCore`` publie ses événements de cycle de vie sur un ``EventBusPort``
   injecté : ``agent.run_start``, ``agent.tool_start`` / ``agent.tool_end``,
   ``agent.run_finished`` (et ``agent.thinking`` quand le mode Réflexion est
   actif) ;
2. un abonné reproduisant le câblage SSE de ``/ask/core/stream`` reconstruit
   EXACTEMENT les frames ``core_tool`` / ``thinking_delta`` historiques — le
   contrat SSE est préservé à travers le port ;
3. un bus PAR RUN isole les flux : aucun cross-talk entre deux runs.

Le LLM et le registre sont des fakes (mêmes patterns que test_agent_core.py).
"""

from __future__ import annotations

from app.agent.core import AgentCore, RunStatus
from app.domain.entities.plan import Intent
from app.infrastructure.events.in_memory import InMemoryEventBus


class ScriptedLLM:
    """LLM fake : renvoie les réponses dans l'ordre, enregistre les messages."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.messages: list[list[dict]] = []

    def call(self, messages):
        self.messages.append(messages)
        return self.replies.pop(0)

    def call_stream(self, messages, on_thinking=None, on_content=None):
        # Diffuse le raisonnement vers le callback quand le mode Réflexion
        # est actif (appelé par le noyau via call_stream).
        if on_thinking is not None:
            on_thinking("réfléchi puis ")
        return self.call(messages)


class FakeRegistry:
    """Registre fake : echo (succès) uniquement, enregistre les appels."""

    def __init__(self):
        self.calls: list[tuple] = []

    def tool_names(self):
        return ["echo"]

    def get(self, tool):
        def echo(*, text=""):
            self.calls.append((tool, text))
            return text
        return echo

    def meta(self, tool):
        return {"description": f"outil {tool}", "required_args": ["text"]}


def _run_core(llm, registry, *, bus=None, **kwargs) -> tuple:
    core = AgentCore(llm, registry, event_bus=bus, **kwargs)
    result = core.run(Intent(prompt="question", session_id="s1"))
    return core, result


# --- Cycle de vie publié sur le bus ------------------------------------------


def test_core_publishes_run_start_and_run_finished():
    bus = InMemoryEventBus()
    llm = ScriptedLLM(["Bonjour ! Je peux t'aider."])
    _run_core(llm, FakeRegistry(), bus=bus)

    types = [e["event_type"] for e in bus.history]
    assert "agent.run_start" in types
    assert "agent.run_finished" in types
    # run_start en premier, run_finished en dernier.
    assert types[0] == "agent.run_start"
    assert types[-1] == "agent.run_finished"

    run_start = next(e for e in bus.history if e["event_type"] == "agent.run_start")
    assert run_start["prompt"] == "question"
    finished = next(e for e in bus.history if e["event_type"] == "agent.run_finished")
    assert finished["status"] == RunStatus.COMPLETED.value
    assert finished["rounds_used"] >= 1


def test_core_publishes_tool_start_and_tool_end_with_fidelity():
    bus = InMemoryEventBus()
    llm = ScriptedLLM([
        '{"plan": [{"tool": "echo", "args": {"text": "salut"}}]}',
        "Voilà : salut.",
    ])
    registry = FakeRegistry()
    result = _run_core(llm, registry, bus=bus,
                       approval_gateway=lambda a: True)[1]
    assert result.status is RunStatus.COMPLETED
    assert result.tool_calls_used == 1

    starts = [e for e in bus.history if e["event_type"] == "agent.tool_start"]
    ends = [e for e in bus.history if e["event_type"] == "agent.tool_end"]
    assert len(starts) == 1
    assert len(ends) == 1
    assert starts[0]["tool"] == "echo"
    assert starts[0]["args"] == {"text": "salut"}
    assert ends[0]["tool"] == "echo"
    assert ends[0]["status"] == "ok"
    assert ends[0]["summary"] == "salut"
    assert ends[0]["duration_ms"] is not None


def test_core_publishes_thinking_when_enabled():
    bus = InMemoryEventBus()
    _run_core(ScriptedLLM(["Bonjour"]), FakeRegistry(), bus=bus, enable_thinking=True)
    thoughts = [e for e in bus.history if e["event_type"] == "agent.thinking"]
    assert thoughts and thoughts[0]["chunk"] == "réfléchi puis "


# --- Reconstruction SSE via le bus (contrat préservé) -------------------------


def _reconstruct_tool_payloads(bus):
    """Répète le câblage final de /ask/core/stream : abonné -> frames core_tool."""
    frames: list[dict] = []

    def on_start(*, tool, args=None, **_e):
        frames.append({"event": "tool_start", "tool": tool, "args": args or {}})

    def on_end(*, tool, status, summary="", error="", duration_ms=None, **_e):
        payload = {"event": "tool_result", "tool": tool,
                   "status": status, "duration_ms": duration_ms}
        payload["summary" if status == "ok" else "error"] = (
            summary if status == "ok" else error)
        frames.append(payload)

    bus.on("agent.tool_start", on_start)
    bus.on("agent.tool_end", on_end)
    return frames


def test_bus_reconstruction_matches_legacy_sse_frames():
    bus = InMemoryEventBus()
    frames = _reconstruct_tool_payloads(bus)
    llm = ScriptedLLM([
        '{"plan": [{"tool": "echo", "args": {"text": "hi"}}]}',
        "Terminé.",
    ])
    result = _run_core(llm, FakeRegistry(), bus=bus,
                       approval_gateway=lambda a: True)[1]
    assert result.status is RunStatus.COMPLETED

    assert frames == [
        {"event": "tool_start", "tool": "echo", "args": {"text": "hi"}},
        {"event": "tool_result", "tool": "echo", "status": "ok",
         "duration_ms": frames[1]["duration_ms"], "summary": "hi"},
    ]


# --- Isolation par run (zéro cross-talk) --------------------------------------


def test_per_run_bus_isolates_flows():
    """Deux runs sur deux bus : le premier ne voit pas les événements du second."""
    bus_a = InMemoryEventBus()
    bus_b = InMemoryEventBus()

    llm_a = ScriptedLLM([
        '{"plan": [{"tool": "echo", "args": {"text": "a"}}]}',
        "Réponse A.",
    ])
    llm_b = ScriptedLLM([
        '{"plan": [{"tool": "echo", "args": {"text": "b"}}]}',
        "Réponse B.",
    ])
    registry_a, registry_b = FakeRegistry(), FakeRegistry()
    _run_core(llm_a, registry_a, bus=bus_a, approval_gateway=lambda a: True)
    _run_core(llm_b, registry_b, bus=bus_b, approval_gateway=lambda a: True)

    a_tools = [e for e in bus_a.history if e["event_type"] == "agent.tool_start"]
    b_tools = [e for e in bus_b.history if e["event_type"] == "agent.tool_start"]
    assert len(a_tools) == 1 and a_tools[0]["args"] == {"text": "a"}
    assert len(b_tools) == 1 and b_tools[0]["args"] == {"text": "b"}
