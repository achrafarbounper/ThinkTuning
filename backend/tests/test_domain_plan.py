# project/tests/test_domain_plan.py
"""Tests des entités agentiques (app/domain/entities/plan.py)."""

import pytest
from pydantic import ValidationError

from app.domain.entities.plan import (
    Action,
    ActionCategory,
    ApprovalDecision,
    ApprovalStatus,
    Decision,
    Intent,
    Plan,
    PlanErrorCode,
    PlanStep,
    PlanValidationReport,
)


def test_intent_valid() -> None:
    intent = Intent(prompt="analyse ce dataset", session_id="sess-1")
    assert intent.role == "default"
    assert intent.max_rounds == 6
    assert intent.created_at.endswith("Z")


def test_intent_rejects_empty_prompt() -> None:
    with pytest.raises(ValidationError):
        Intent(prompt="", session_id="s")


def test_action_fingerprint_deterministic() -> None:
    a1 = Action(tool="read_file", args={"path": "a.txt"}, category=ActionCategory.READ)
    a2 = Action(tool="read_file", args={"path": "a.txt"}, category=ActionCategory.READ)
    assert a1.fingerprint() == a2.fingerprint()
    assert len(a1.fingerprint()) == 64


def test_plan_rejects_duplicate_task_ids() -> None:
    step = PlanStep(task_id="t1", subtask="x")
    with pytest.raises(ValidationError, match="dupliqu"):
        Plan(steps=[step, step.model_copy()])


def test_plan_rejects_self_dependency() -> None:
    with pytest.raises(ValidationError, match="elle-même"):
        PlanStep(task_id="t1", dependencies=["t1"])


def test_plan_cycle_detection() -> None:
    plan = Plan(
        steps=[
            PlanStep(task_id="a", dependencies=["b"]),
            PlanStep(task_id="b", dependencies=["a"]),
        ]
    )
    assert plan.has_cycle()
    with pytest.raises(ValueError, match="cyclique"):
        plan.topological_order()


def test_plan_topological_order() -> None:
    plan = Plan(
        steps=[
            PlanStep(task_id="c", dependencies=["b"]),
            PlanStep(task_id="b", dependencies=["a"]),
            PlanStep(task_id="a"),
        ]
    )
    assert not plan.has_cycle()
    assert plan.topological_order() == ["a", "b", "c"]


def test_plan_undefined_dependencies() -> None:
    plan = Plan(steps=[PlanStep(task_id="a", dependencies=["ghost"])])
    assert plan.dependencies_undefined() == ["ghost"]


def test_approval_decision_flow() -> None:
    action = Action(tool="write_file", args={"path": "x.txt"})
    decision = ApprovalDecision(approval_id="appr-1", action=action)
    assert not decision.is_resolved
    granted = decision.model_copy(update={"status": ApprovalStatus.APPROVED, "decided_by": "human"})
    assert granted.is_resolved and granted.is_granted
    # Frozen : mutation directe interdite
    with pytest.raises(ValidationError):
        decision.status = ApprovalStatus.REJECTED  # type: ignore[misc]


def test_validation_report_failure_shape() -> None:
    report = PlanValidationReport(ok=False, error_code=PlanErrorCode.PLAN_CYCLE, message="cycle")
    payload = report.to_dict()
    assert payload == {"ok": False, "error_code": PlanErrorCode.PLAN_CYCLE, "message": "cycle"}


def test_decision_values_match_legacy() -> None:
    """Compatibilité stricte avec ia/agent/approvals.py et approval_store."""
    assert Decision.AUTO_APPROVE.value == "auto_approve"
    assert Decision.APPROVE.value == "approve"
    assert Decision.REJECT.value == "reject"
    assert ApprovalStatus.PENDING.value == "pending"
    assert ApprovalStatus.APPROVED.value == "approved"
    assert ApprovalStatus.REJECTED.value == "rejected"
