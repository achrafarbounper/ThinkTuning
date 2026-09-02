# project/tests/test_agent_budget.py
"""Tests de la policy de budget (app/agent/policies/budget.py)."""

import pytest

from app.agent.policies.budget import RunBudget
from app.domain.errors import BudgetExceededError


def test_consume_llm_round_returns_1_based_index() -> None:
    budget = RunBudget(max_llm_rounds=3, max_tool_calls=5)
    assert budget.consume_llm_round() == 1
    assert budget.consume_llm_round() == 2
    assert budget.consume_llm_round() == 3
    assert budget.llm_rounds_left == 0


def test_llm_budget_exhaustion_raises() -> None:
    budget = RunBudget(max_llm_rounds=2, max_tool_calls=5)
    budget.consume_llm_round()
    budget.consume_llm_round()
    with pytest.raises(BudgetExceededError, match="2/2"):
        budget.consume_llm_round()


def test_tool_budget_exhaustion_raises_with_tool_name() -> None:
    budget = RunBudget(max_llm_rounds=5, max_tool_calls=1)
    budget.consume_tool_call("read_file")
    with pytest.raises(BudgetExceededError, match="read_file"):
        budget.consume_tool_call("read_file")


def test_snapshot_reflects_consumption() -> None:
    budget = RunBudget(max_llm_rounds=6, max_tool_calls=20)
    budget.consume_llm_round()
    budget.consume_tool_call("now")
    snap = budget.snapshot().to_dict()
    assert snap == {
        "llm_rounds_used": 1,
        "llm_rounds_max": 6,
        "tool_calls_used": 1,
        "tool_calls_max": 20,
    }
    assert not budget.exhausted


def test_exhausted_property() -> None:
    budget = RunBudget(max_llm_rounds=1, max_tool_calls=1)
    assert not budget.exhausted
    budget.consume_llm_round()
    assert budget.exhausted  # un seul axe épuisé suffit à bloquer


def test_invalid_limits_rejected() -> None:
    with pytest.raises(ValueError, match="plafonds"):
        RunBudget(max_llm_rounds=0)
    with pytest.raises(ValueError, match="plafonds"):
        RunBudget(max_tool_calls=-1)


def test_error_payload_carries_snapshot() -> None:
    budget = RunBudget(max_llm_rounds=1, max_tool_calls=1)
    budget.consume_llm_round()
    try:
        budget.consume_llm_round()
    except BudgetExceededError as exc:
        payload = exc.to_payload()["error"]
        assert payload["code"] == "budget_exceeded"
        assert payload["details"]["llm_rounds_used"] == 1
    else:
        pytest.fail("BudgetExceededError attendu")
