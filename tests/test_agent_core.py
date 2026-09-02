# project/tests/test_agent_core.py
"""Tests de la boucle agentique (app/agent/core.py) — LLM entièrement fake."""

import pytest

from app.agent.core import AgentCore, RunStatus, build_system_prompt, extract_plan
from app.domain.entities.plan import Intent


class ScriptedLLM:
    """LLM fake : renvoie les réponses dans l'ordre, enregistre les messages."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.messages: list[list[dict]] = []

    def call(self, messages):
        self.messages.append(messages)
        return self.replies.pop(0)

    def call_stream(self, messages, on_thinking=None, on_content=None):
        return self.call(messages)


class FakeRegistry:
    """Registre fake : now (lecture), echo, read_file (échec simulé)."""

    def tool_names(self):
        return ["now", "echo", "read_file"]

    def get(self, tool):
        table = {
            "now": lambda: "2026-09-02T12:00:00Z",
            "echo": lambda text="": text,
            "read_file": lambda path="": (_ for _ in ()).throw(RuntimeError("panne simulée")),
        }
        return table.get(tool)

    def meta(self, tool):
        return {"description": f"outil {tool}", "required_args": []}


@pytest.fixture()
def registry() -> FakeRegistry:
    return FakeRegistry()


def intent(**kwargs) -> Intent:
    return Intent(prompt="question", session_id="s1", **kwargs)


# --- Parsing -------------------------------------------------------------------


def test_extract_plan_forms() -> None:
    assert extract_plan('{"plan": [{"tool": "now", "args": {}}]}') is not None
    assert extract_plan('[{"tool": "echo", "args": {"text": "x"}}]') is not None
    assert extract_plan('Voici:\n```json\n{"tasks": [{"tool": "now"}]}\n```') is not None
    assert extract_plan("Réponse en texte normal sans JSON.") is None
    assert extract_plan("") is None


def test_extract_plan_rejects_entries_without_tool() -> None:
    assert extract_plan('{"plan": [{"subtask": "sans outil"}]}') is None


def test_system_prompt_lists_real_tools(registry) -> None:
    prompt = build_system_prompt(registry)
    assert "now" in prompt and "echo" in prompt and "read_file" in prompt
    assert "PROTOCOLE" in prompt


# --- Boucle ----------------------------------------------------------------------


def test_direct_text_answer(registry) -> None:
    llm = ScriptedLLM(["Bonjour ! Comment puis-je aider ?"])
    result = AgentCore(llm, registry).run(intent())
    assert result.status is RunStatus.COMPLETED
    assert result.answer == "Bonjour ! Comment puis-je aider ?"
    assert result.actions == []
    assert result.rounds_used == 1


def test_tool_call_then_final_answer(registry) -> None:
    llm = ScriptedLLM([
        '{"plan": [{"tool": "now", "args": {}}]}',
        "Il est 12h00 UTC.",
    ])
    result = AgentCore(llm, registry).run(intent())
    assert result.status is RunStatus.COMPLETED
    assert result.answer == "Il est 12h00 UTC."
    assert len(result.actions) == 1
    assert result.actions[0].status == "done"
    assert result.actions[0].decision == "auto_approve"
    assert result.tool_calls_used == 1
    # Le 2e appel LLM contient le résultat de l'outil (fidélité au résultat).
    second = llm.messages[1]
    assert any("2026-09-02T12:00:00Z" in m["content"] for m in second)

def test_unknown_tool_self_correction(registry) -> None:
    llm = ScriptedLLM([
        '{"plan": [{"tool": "outil_invente", "args": {}}]}',
        '{"plan": [{"tool": "now", "args": {}}]}',
        "Voilà l'heure.",
    ])
    result = AgentCore(llm, registry).run(intent())
    assert result.status is RunStatus.COMPLETED
    assert result.answer == "Voilà l'heure."
    assert result.actions[0].status == "error"
    assert result.actions[1].status == "done"
    # Le message d'erreur contient la liste des outils valides.
    second = llm.messages[1]
    assert any("now, echo, read_file" in m["content"] for m in second)


def test_tool_failure_self_correction(registry) -> None:
    llm = ScriptedLLM([
        '{"plan": [{"tool": "read_file", "args": {"path": "x.csv"}}]}',
        "L'outil a échoué, voici une explication alternative.",
    ])
    result = AgentCore(llm, registry).run(intent())
    assert result.status is RunStatus.COMPLETED
    assert result.actions[0].status == "error"
    assert "panne simulée" in result.actions[0].error


def test_hard_reject_then_answer(registry) -> None:
    llm = ScriptedLLM([
        '{"plan": [{"tool": "write_file", "args": {"path": ".env"}}]}',
        "Je ne peux pas modifier ce fichier.",
    ])
    result = AgentCore(llm, registry).run(intent())
    assert result.status is RunStatus.COMPLETED
    assert result.actions[0].status == "rejected"
    assert result.actions[0].decision == "reject"


def test_reject_loop_stops_run(registry) -> None:
    llm = ScriptedLLM([
        '{"plan": [{"tool": "write_file", "args": {"path": ".env"}}]}',
        '{"plan": [{"tool": "write_file", "args": {"path": ".env"}}]}',
    ])
    result = AgentCore(llm, registry).run(intent())
    assert result.status is RunStatus.REJECTED_LOOP

def test_pending_approval_without_gateway(registry) -> None:
    llm = ScriptedLLM(['{"plan": [{"tool": "echo", "args": {"text": "x"}}]}'])
    result = AgentCore(llm, registry).run(intent())
    assert result.status is RunStatus.PENDING_APPROVAL
    assert result.awaiting_action is not None
    assert result.awaiting_action.tool == "echo"
    assert result.actions[0].status == "awaiting_approval"


def test_approval_gateway_granted_executes(registry) -> None:
    llm = ScriptedLLM([
        '{"plan": [{"tool": "echo", "args": {"text": "x"}}]}',
        "Terminé.",
    ])
    approvals: list[str] = []

    def gateway(action) -> bool:
        approvals.append(action.tool)
        return True

    result = AgentCore(llm, registry, approval_gateway=gateway).run(intent())
    assert result.status is RunStatus.COMPLETED
    assert result.actions[0].status == "done"
    assert result.actions[0].decision == "approve"
    assert approvals == ["echo"]


def test_llm_budget_exhaustion(registry) -> None:
    llm = ScriptedLLM(['{"plan": [{"tool": "now"}]}'] * 10)
    result = AgentCore(llm, registry).run(intent(max_rounds=2))
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert result.rounds_used == 2


def test_tool_budget_exhaustion(registry) -> None:
    llm = ScriptedLLM(['{"plan": [{"tool": "now"}, {"tool": "echo", "args": {"text": "a"}}]}'])
    result = AgentCore(llm, registry, max_tool_calls=1).run(intent())
    assert result.status is RunStatus.BUDGET_EXHAUSTED
    assert result.tool_calls_used == 1
    assert result.actions[0].status == "done"


def test_run_never_raises_on_llm_crash(registry) -> None:
    class CrashingLLM:
        def call(self, messages):
            raise RuntimeError("LLM down")

        def call_stream(self, messages, on_thinking=None, on_content=None):
            return self.call(messages)

    result = AgentCore(CrashingLLM(), registry).run(intent())
    assert result.status is RunStatus.FAILED
