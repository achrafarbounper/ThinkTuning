"""Tests du pipeline « propositions de tools » (SCRUM-99).

Plan validator (pseudo-rôle ``propose_tool``), règles de prompt du planner,
codes d'erreur dédiés et surcharge « registry » du gate d'approbation.
Lance : pytest tests/test_tool_proposal.py -v
"""

import json

import pytest

from ia.agent.approvals import Decision, classify
from ia.agent.errors import TOOL_PROPOSAL_LIMITED, TOOL_PROPOSAL_REJECTED, bucket_of
from ia.agent.plan_validator import TOOL_PROPOSAL_ROLE, validate_plan
from ia.agent.prompts import build_planner_prompt
from ia.tools.registry import get_global_registry
from ia.tools.tool_registry import TOOLS

ROLES = ["web", "files", "ml", "data", "math", "ops", "shell", "docker",
         "operator", "developer", "reviewer"]

TOOL_DEF = {
    "name": "get_weather",
    "description": "Météo d'une ville.",
    "category": "api",
    "required_args": ["city"],
    "parameters": {"city": {"type": "string", "required": True}},
}


_OMIT = object()  # sentinel : « pas de clé tool » (≠ tool=None)


def _plan_with_tool(tool=_OMIT, task_extra=""):
    if tool is _OMIT:
        tool = TOOL_DEF
    return json.dumps({
        "tasks": [
            {"task_id": "t1", "role": "web", "subtask": "cherche la réponse"},
            {
                "task_id": "p1",
                "role": TOOL_PROPOSAL_ROLE,
                "subtask": "capacité manquante",
                **({"tool": tool} if tool is not _OMIT else {}),
                **({"extra": task_extra} if task_extra else {}),
            },
        ]
    })


# --- plan_validator --------------------------------------------------------------

def test_propose_tool_is_extracted_not_dispatched():
    result = validate_plan(_plan_with_tool(), ROLES)
    assert result.ok
    assert [t.task_id for t in result.tasks] == ["t1"]  # le pseudo-rôle n'est PAS dispatché
    assert len(result.tool_proposals) == 1
    proposal = result.tool_proposals[0]
    assert proposal["name"] == "get_weather"
    assert proposal["task_id"] == "p1"
    assert proposal["parameters"]["city"]["required"] is True
    payload = result.to_dict()
    assert payload["tool_proposals"][0]["name"] == "get_weather"


def test_proposal_only_plan_is_valid():
    plan = json.dumps({
        "tasks": [{
            "task_id": "p1", "role": TOOL_PROPOSAL_ROLE,
            "subtask": "pourquoi", "tool": TOOL_DEF,
        }],
    })
    result = validate_plan(plan, ROLES)
    assert result.ok and not result.tasks and len(result.tool_proposals) == 1


def test_second_proposal_is_limited():
    second = {**TOOL_DEF, "name": "get_news"}
    result = validate_plan(
        json.dumps({
            "tasks": [
                {"task_id": "p1", "role": TOOL_PROPOSAL_ROLE,
                 "subtask": "a", "tool": TOOL_DEF},
                {"task_id": "p2", "role": TOOL_PROPOSAL_ROLE,
                 "subtask": "b", "tool": second},
            ],
        }),
        ROLES,
        max_tool_proposals=1,
    )
    assert result.ok and len(result.tool_proposals) == 1
    assert result.tool_proposal_notes
    assert "plafond" in result.tool_proposal_notes[0]["reason"]


def test_duplicate_proposal_note():
    plan = json.dumps({
        "tasks": [
            {"task_id": "p1", "role": TOOL_PROPOSAL_ROLE,
             "subtask": "a", "tool": TOOL_DEF},
            {"task_id": "p2", "role": TOOL_PROPOSAL_ROLE,
             "subtask": "b", "tool": TOOL_DEF},
        ],
    })
    result = validate_plan(plan, ROLES, max_tool_proposals=5)
    assert len(result.tool_proposals) == 1
    assert any("déjà proposé" in n["reason"] for n in result.tool_proposal_notes)


@pytest.mark.parametrize(
    "tool",
    [
        {**TOOL_DEF, "name": "GetWeather"},   # nom invalide
        {k: v for k, v in TOOL_DEF.items() if k != "description"},  # sans description
        {"name": "no_params_tool", "description": "x", "parameters": "pas un dict"},
    ],
)
def test_invalid_proposals_go_to_notes(tool):
    result = validate_plan(_plan_with_tool(tool=tool), ROLES)
    assert result.ok  # le plan continue SANS le tool (fail-closed, bucket failed)
    assert not result.tool_proposals
    assert result.tool_proposal_notes and result.tool_proposal_notes[0]["reason"]


def test_missing_tool_object_note():
    result = validate_plan(_plan_with_tool(tool=None), ROLES)
    assert result.ok and not result.tool_proposals
    assert any("tool" in n["reason"] for n in result.tool_proposal_notes)


# --- codes d'erreur ---------------------------------------------------------------



# --- prompts ----------------------------------------------------------------------

def test_planner_prompt_without_proposals_has_no_tool_block():
    prompt = build_planner_prompt("demande", ROLES)
    assert "OUTILS PERSONNALISÉS" not in prompt
    assert "propose_tool" not in prompt


def test_planner_prompt_with_proposals_describes_pipeline():
    prompt = build_planner_prompt(
        "demande", ROLES, allow_tool_proposals=True, max_tool_proposals=2,
    )
    assert "OUTILS PERSONNALISÉS" in prompt
    assert "propose_tool" in prompt
    assert "2" in prompt  # plafond injecté


# --- approvals : surcharge « registry » (fail-closed) ------------------------------

@pytest.fixture()
def cleanup_tool():
    yield
    registry = get_global_registry()
    registry.remove_tool("dyn_gate_tool")


def test_registry_approval_ignores_native_tools():
    from ia.agent.approvals import _registry_approval
    assert _registry_approval("run_command") is None
    assert _registry_approval("outil_absent") is None


def test_dynamic_tool_manual_by_default(cleanup_tool):
    from ia.agent.approvals import _registry_approval
    get_global_registry().add_tool(
        lambda **kw: "ok",
        {
            "name": "dyn_gate_tool", "description": "gate",
            "required_args": [], "parameters": {},
            "safety": {"level": "safe", "requires_approval": False},
        },
        allow_auto_approval=False,  # source non humaine (planner) : forcé manual
    )
    assert _registry_approval("dyn_gate_tool") == Decision.APPROVE
    decision = classify("dyn_gate_tool", {})
    assert decision.decision == Decision.APPROVE
    assert decision.timestamp


def test_dynamic_tool_blocked_when_dangerous(cleanup_tool):
    from ia.agent.approvals import _registry_approval
    get_global_registry().add_tool(
        lambda **kw: "ok",
        {
            "name": "dyn_gate_tool", "description": "gate",
            "required_args": [], "parameters": {},
            "safety": {"level": "dangerous", "requires_approval": True},
        },
    )
    assert _registry_approval("dyn_gate_tool") == Decision.REJECT
    assert classify("dyn_gate_tool", {}).decision == Decision.REJECT


def test_dynamic_tool_auto_only_for_human_source(cleanup_tool):
    from ia.agent.approvals import _registry_approval
    get_global_registry().add_tool(
        lambda **kw: "ok",
        {
            "name": "dyn_gate_tool", "description": "gate",
            "required_args": [], "parameters": {},
            "safety": {"level": "safe", "requires_approval": False},
        },
        allow_auto_approval=True,  # source humaine (API) : safety honorée
    )
    assert _registry_approval("dyn_gate_tool") == Decision.AUTO_APPROVE
    assert classify("dyn_gate_tool", {}).decision == Decision.AUTO_APPROVE


def test_proposal_never_self_registers():
    """Le pipeline ne modifie JAMAIS le registre de lui-même (fail-closed)."""
    before = set(TOOLS)
    result = validate_plan(_plan_with_tool(), ROLES)
    assert result.ok and result.tool_proposals
    assert set(TOOLS) == before  # aucune inscription automatique


def test_tool_proposal_codes_in_failed_bucket():
    assert bucket_of(TOOL_PROPOSAL_REJECTED) == "failed"
    assert bucket_of(TOOL_PROPOSAL_LIMITED) == "failed"
