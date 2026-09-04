"""Tests offline du validateur de plan de l'orchestration multi-agents.

Couvre le plan_validator (les contraintes DÉTERMINISTES qui imposent un format
stable au plan produit par le superviseur LLM) :
    - extraction JSON tolérante (liste directe, {tasks:[...]}, prose/fences) ;
    - rôle inexistant -> PlanValidationFailed ;
    - plan non-JSON / vide -> échec (PlanValidationFailed / PlanEmpty) ;
    - sous-tâche vide / non textuelle ;
    - duplication de task_id et de sous-tâche (rôle, texte) ;
    - circularité de dépendances -> PlanCycle ;
    - borne max_roles ;
    - sortie ok=True avec task_id normalisés et dependencies filtrées.

Aucun réseau : pur Python. Lance avec : pytest tests/test_plan_validator.py -v
"""

import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from ia.agent.errors import (  # noqa: E402
    PLAN_CYCLE,
    PLAN_EMPTY,
    PLAN_VALIDATION_FAILED,
    TASK_INVALID,
)
from ia.agent.plan_validator import validate_plan  # noqa: E402

ROLES = ["web", "files", "data", "math"]


# --- Plans valides -----------------------------------------------------------

def test_valid_plan_direct_list():
    raw = json.dumps([
        {"task_id": "t1", "role": "web", "subtask": "Cherche le titre"},
        {"task_id": "t2", "role": "data", "subtask": "Compte les lignes"},
    ])
    res = validate_plan(raw, ROLES, max_roles=5)
    assert res.ok is True
    assert [t.role for t in res.tasks] == ["web", "data"]
    assert res.tasks[0].subtask == "Cherche le titre"


def test_valid_plan_wrapped_in_tasks_key():
    raw = json.dumps({"tasks": [
        {"role": "math", "subtask": "Calcule 2+2", "dependencies": []},
    ]})
    res = validate_plan(raw, ROLES, max_roles=5)
    assert res.ok is True
    assert res.tasks[0].role == "math"
    # task_id auto-attribué (non fourni) + dependencies filtrées en liste.
    assert res.tasks[0].task_id.startswith("task-")
    assert res.tasks[0].dependencies == []


def test_valid_plan_ignores_prose_around_json():
    raw = (
        "Voici le plan :\n```json\n"
        '[{"role": "math", "subtask": "Résous x"}]'
        "\n```"
    )
    res = validate_plan(raw, ROLES, max_roles=5)
    assert res.ok is True
    assert res.tasks[0].role == "math"


# --- Plans invalides ----------------------------------------------------------

def test_non_json_plan_fails():
    res = validate_plan("Pas de JSON ici du tout.", ROLES, max_roles=5)
    assert res.ok is False
    assert res.error_code == PLAN_VALIDATION_FAILED


def test_empty_plan_fails():
    res = validate_plan('[]', ROLES, max_roles=5)
    assert res.ok is False
    assert res.error_code == PLAN_EMPTY


def test_unknown_role_fails():
    raw = json.dumps([{"role": "h4ck3r", "subtask": "prende le contrôle"}])
    res = validate_plan(raw, ROLES, max_roles=5)
    assert res.ok is False
    assert res.error_code == TASK_INVALID
    assert any("h4ck3r" in m for m in res.message.split(";"))


def test_empty_subtask_fails():
    raw = json.dumps([{"role": "web", "subtask": "   "}])
    res = validate_plan(raw, ROLES, max_roles=5)
    assert res.ok is False
    assert res.error_code == TASK_INVALID


def test_duplicate_role_subtask_fails():
    raw = json.dumps([
        {"role": "web", "subtask": "Même tâche"},
        {"role": "web", "subtask": "Même tâche"},
    ])
    res = validate_plan(raw, ROLES, max_roles=5)
    assert res.ok is False
    assert res.error_code == TASK_INVALID
    assert "dupliquée" in res.message


def test_duplicate_task_id_fails():
    raw = json.dumps([
        {"task_id": "x", "role": "web", "subtask": "Une"},
        {"task_id": "x", "role": "data", "subtask": "Autre"},
    ])
    res = validate_plan(raw, ROLES, max_roles=5)
    assert res.ok is False
    assert res.error_code == TASK_INVALID
    assert "dupliqué" in res.message


def test_exceeding_max_roles_fails():
    raw = json.dumps([
        {"role": "web", "subtask": f"t{n}"} for n in range(6)
    ])
    res = validate_plan(raw, ROLES, max_roles=5)
    assert res.ok is False
    assert res.error_code == PLAN_VALIDATION_FAILED


def test_dependency_cycle_fails():
    # t1 <- t2 <- t1 : cycle.
    raw = json.dumps([
        {"task_id": "a", "role": "web", "subtask": "A", "dependencies": ["b"]},
        {"task_id": "b", "role": "web", "subtask": "B", "dependencies": ["a"]},
    ])
    res = validate_plan(raw, ROLES, max_roles=5)
    assert res.ok is False
    assert res.error_code == PLAN_CYCLE


def test_acyclic_dependencies_ok():
    raw = json.dumps([
        {"task_id": "a", "role": "web", "subtask": "A"},
        {"task_id": "b", "role": "web", "subtask": "B", "dependencies": ["a"]},
    ])
    res = validate_plan(raw, ROLES, max_roles=5)
    assert res.ok is True
