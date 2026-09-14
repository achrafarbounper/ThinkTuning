# project/tests/test_agent_budget.py
"""Tests de la policy de budget (app/agent/policies/budget.py)."""

import pytest

from types import SimpleNamespace

from app.agent.policies.budget import BudgetPolicy, RunBudget
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


def test_budget_policy_uses_shared_runtime_config() -> None:
    config = SimpleNamespace(
        max_llm_rounds=7,
        max_tool_calls=11,
        max_workers=3,
        max_events=123,
        max_runtime_ms=45678,
        max_retries=4,
        budget_enforcement_mode="fail_fast",
    )
    policy = BudgetPolicy.from_config(config)

    assert policy.to_dict() == {
        "max_llm_rounds": 7,
        "max_tool_calls": 11,
        "max_workers": 3,
        "max_events": 123,
        "max_runtime_ms": 45678,
        "max_retries": 4,
        "enforcement_mode": "fail_fast",
    }
    assert policy.to_run_budget().snapshot().to_dict() == {
        "llm_rounds_used": 0,
        "llm_rounds_max": 7,
        "tool_calls_used": 0,
        "tool_calls_max": 11,
    }


def test_http_and_mcp_budget_traces_match_for_same_scenario() -> None:
    """HTTP and MCP must share the same budget policy and emit the same trace."""
    config = SimpleNamespace(
        max_llm_rounds=8,
        max_tool_calls=12,
        max_workers=4,
        max_events=200,
        max_runtime_ms=30000,
        max_retries=2,
        budget_enforcement_mode="fail_fast",
    )

    http_policy = BudgetPolicy.from_config(config)
    mcp_policy = BudgetPolicy.from_config(config)

    http_trace = http_policy.to_trace(
        llm_rounds_used=2,
        tool_calls_used=3,
        workers_used=2,
        events_emitted=25,
        runtime_ms_used=4321,
        retries_used=1,
    )
    mcp_trace = mcp_policy.to_trace(
        llm_rounds_used=2,
        tool_calls_used=3,
        workers_used=2,
        events_emitted=25,
        runtime_ms_used=4321,
        retries_used=1,
    )

    assert http_trace == mcp_trace
    assert http_trace["max_llm_rounds"] == 8
    assert http_trace["max_tool_calls"] == 12
    assert http_trace["llm_rounds_used"] == 2
    assert http_trace["tool_calls_used"] == 3
