"""Tests orchestrateur × tools personnalisés (SCRUM-99).

Pipeline proposer → relire (reviewer), interrupteur ENV ``USE_DYNAMIC_TOOLS``,
injection de la ``ToolRegistry`` dans les workers et fail-closed (aucun
enregistrement automatique). Lance : pytest tests/test_multi_agent_tools.py -v
"""

import json

from ia.agent.orchestrator import (
    EV_TOOL_PROPOSED,
    EV_TOOL_REVIEWED,
    MultiAgentCoordinator,
    build_role_agent,
)
from ia.tools.registry import ToolRegistry
from ia.tools.tool_registry import TOOLS

PLAN_WITH_PROPOSAL = json.dumps({
    "tasks": [
        {"task_id": "t1", "role": "web", "subtask": "cherche la météo"},
        {
            "task_id": "p1",
            "role": "propose_tool",
            "subtask": "capacité météo manquante",
            "tool": {
                "name": "get_weather",
                "description": "Météo d'une ville.",
                "category": "api",
                "required_args": ["city"],
                "parameters": {"city": {"type": "string", "required": True}},
            },
        },
    ]
})


class FakeResult:
    def __init__(self, answer: str):
        self.answer = answer


class FakeAgent:
    """Agent scripté : mémorise les prompts, dépile les réponses."""

    def __init__(self, role, replies):
        self.role = role
        self.replies = list(replies)
        self.prompts: list[str] = []

    def run_detailed(self, prompt, on_thinking=None, on_tool_event=None, **_):
        self.prompts.append(prompt)
        if not self.replies:
            return FakeResult(f"[{self.role}] réponse par défaut")
        return FakeResult(self.replies.pop(0))


def _builder(lead_replies, worker_results, reviewer_replies):
    built = {}

    def _build(role):
        if role in built:
            return built[role]
        if role == "lead":
            agent = FakeAgent("lead", list(lead_replies))
        elif role == "reviewer":
            agent = FakeAgent("reviewer", list(reviewer_replies))
        else:
            agent = FakeAgent(role, [worker_results.get(role, f"résultat {role}")])
        built[role] = agent
        return agent

    return _build, built


def _collect_events():
    events = []

    def on_event(event_type, data):
        events.append((event_type, data))

    return events, on_event


# --- Pipeline proposer → relire ---------------------------------------------------

def test_approved_proposal_returns_to_human_without_registering():
    build, built = _builder(
        [PLAN_WITH_PROPOSAL, "Synthèse finale."],
        {"web": "INFO-WEB"},
        ['{"verdict": "approve", "reason": "nécessaire et borné"}'],
    )
    events, on_event = _collect_events()
    outcome = MultiAgentCoordinator(
        "fake-llm", role_builder=build, enable_tool_proposals=True,
    ).run("demande météo", on_event=on_event)

    assert outcome["status"] == "completed"
    proposals = outcome["tool_proposals"]
    assert len(proposals) == 1
    assert proposals[0]["status"] == "approved"
    assert proposals[0]["reason"] == "nécessaire et borné"
    # Événements proposé + reviewed (approved) émis :
    types = [e for e, _ in events]
    assert EV_TOOL_PROPOSED in types
    reviewed = [d for e, d in events if e == EV_TOOL_REVIEWED]
    assert reviewed and reviewed[0]["decision"] == "approved"
    # FAIL-CLOSED : aucun enregistrement automatique dans le registre.
    assert "get_weather" not in TOOLS


def test_rejected_proposal_carries_error_code():
    build, _ = _builder(
        [PLAN_WITH_PROPOSAL, "Synthèse finale."],
        {"web": "INFO-WEB"},
        ['{"verdict": "reject", "reason": "couvert par web_fetch"}'],
    )
    events, on_event = _collect_events()
    outcome = MultiAgentCoordinator(
        "fake-llm", role_builder=build, enable_tool_proposals=True,
    ).run("demande météo", on_event=on_event)

    proposal = outcome["tool_proposals"][0]
    assert proposal["status"] == "rejected"
    assert proposal["error_code"] == "ToolProposalRejected"
    assert "web_fetch" in proposal["reason"]
    reviewed = [d for e, d in events if e == EV_TOOL_REVIEWED]
    assert reviewed[0]["decision"] == "rejected"
    assert "get_weather" not in TOOLS


def test_unreadable_verdict_is_fail_closed():
    build, _ = _builder(
        [PLAN_WITH_PROPOSAL, "Synthèse finale."],
        {"web": "INFO-WEB"},
        ["Je ne comprends pas la demande."],  # verdict illisible
    )
    outcome = MultiAgentCoordinator(
        "fake-llm", role_builder=build, enable_tool_proposals=True,
    ).run("demande météo")
    proposal = outcome["tool_proposals"][0]
    assert proposal["status"] == "rejected"
    assert "illisible" in proposal["reason"]


# --- Interrupteur ENV USE_DYNAMIC_TOOLS -------------------------------------------

def test_env_off_forces_legacy(monkeypatch):
    """``USE_DYNAMIC_TOOLS=false`` : strict legacy (pipeline désactivé)."""
    monkeypatch.setenv("USE_DYNAMIC_TOOLS", "false")
    build, built = _builder(
        [PLAN_WITH_PROPOSAL, "Synthèse finale."],
        {"web": "INFO-WEB"},
        ['{"verdict": "approve", "reason": "ok"}'],
    )
    events, on_event = _collect_events()
    outcome = MultiAgentCoordinator(
        "fake-llm", role_builder=build, enable_tool_proposals=True,
    ).run("demande météo", on_event=on_event)

    assert outcome["status"] == "completed"
    assert "tool_proposals" not in outcome
    assert not [e for e, _ in events if e.startswith("agent.tool")]
    assert "reviewer" not in built  # aucune relecture instanciée
    assert "get_weather" not in TOOLS


def test_proposals_disabled_by_default():
    """Sans ``enable_tool_proposals`` : comportement historique strict."""
    build, _ = _builder(
        [PLAN_WITH_PROPOSAL, "Synthèse finale."],
        {"web": "INFO-WEB"},
        [],
    )
    events, on_event = _collect_events()
    outcome = MultiAgentCoordinator("fake-llm", role_builder=build).run(
        "demande météo", on_event=on_event,
    )
    assert outcome["status"] == "completed"
    assert "tool_proposals" not in outcome
    assert not [e for e, _ in events if e.startswith("agent.tool")]


# --- Injection de la ToolRegistry ---------------------------------------------------

def test_registry_injection_extends_workers(monkeypatch):
    """``tool_registry`` injectée : les workers voient natifs + dynamiques."""
    captured = {}

    def fake_build_role_agent(role_name, llm_client, tools_map, required_map, **kw):
        captured[role_name] = dict(tools_map)
        return FakeAgent(role_name, [f"réponse {role_name}"])

    monkeypatch.setattr(
        "ia.agent.orchestrator.build_role_agent", fake_build_role_agent,
    )
    registry = ToolRegistry()
    registry.add_tool(
        lambda **kw: "ok",
        {
            "name": "dyn_worker_tool", "description": "injection",
            "required_args": ["q"],
            "parameters": {"q": {"type": "string", "required": True}},
        },
    )
    coordinator = MultiAgentCoordinator("fake-llm", tool_registry=registry)
    coordinator._make_agent("web")
    worker_tools = captured["web"]
    assert "dyn_worker_tool" in worker_tools      # dynamique
    assert "run_command" in worker_tools          # natif
    try:
        TOOLS.pop("dyn_worker_tool", None)
    finally:
        registry.remove_tool("dyn_worker_tool")


def test_real_build_role_agent_gives_reviewer_no_tools():
    """Le rôle « reviewer » (aucun outil) ne peut rien exécuter."""

    class _NoLLM:
        pass

    agent = build_role_agent("reviewer", _NoLLM(), TOOLS, {})
    assert agent._tools == {}  # gate de rôle : aucun appel d'outil possible
