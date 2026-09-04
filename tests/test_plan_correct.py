"""Tests du correcteur déterministe de plans (planner hybride, règles dures).

Couvre ``ia/agent/plan_correct.py`` :
    - diagnostics → rôle « ops » (JAMAIS shell pour du CPU simple) ;
    - web → rôle « web » ;
    - SQL mutant → rejet immédiat (PlanRejected) ;
    - shell → UNIQUEMENT si commande whitelistée (sinon PlanRejected) ;
    - lecture seule → marquage ``auto_approve`` (réduction des approvals) ;
    - le prompt du planner expose les capacités RÉELLES des rôles.

Aucun réseau : tout est déterministe. Lance : pytest tests/test_plan_correct.py -v
"""

import pytest

from ia.agent.plan_correct import PlanRejected, correct_plan, note_auto_approved
from ia.agent.prompts import build_planner_prompt
from ia.agent.roles import ROLES, role_tools


# --- Diagnostics → ops --------------------------------------------------------


def test_diagnostic_subtask_is_retargeted_to_ops():
    plan = [{"task_id": "task-1", "role": "shell", "subtask": "Diagnostic : combien de RAM et de disque ?"}]
    corrected = correct_plan(plan)
    assert corrected[0]["role"] == "ops"
    assert corrected[0]["auto_approve"] is True


def test_diagnostic_via_files_is_retargeted_to_ops():
    plan = [{"task_id": "t", "role": "files", "subtask": "Donne la version de python et le CPU."}]
    assert correct_plan(plan)[0]["role"] == "ops"


def test_ops_diagnostic_is_kept_and_auto_approved():
    plan = [{"task_id": "t", "role": "ops", "subtask": "Infos GPU et mémoire disponibles."}]
    corrected = correct_plan(plan)
    assert corrected[0]["role"] == "ops"
    assert corrected[0]["auto_approve"] is True


def test_roles_registry_ops_has_gpu_info():
    # Faiblesse #3 : le rôle ops couvre env_info / disk_usage / gpu_info.
    assert {"env_info", "disk_usage", "gpu_info"} <= set(ROLES["ops"].tools)
    assert "gpu_info" in role_tools()["ops"]


# --- Web ----------------------------------------------------------------------


def test_web_subtask_is_retargeted_to_web():
    plan = [{"task_id": "t", "role": "ops", "subtask": "Cherche sur le web les dernières actualités IA."}]
    assert correct_plan(plan)[0]["role"] == "web"


def test_web_role_is_kept():
    plan = [{"task_id": "t", "role": "web", "subtask": "Recherche web sur le modèle distilbert."}]
    assert correct_plan(plan)[0]["role"] == "web"


# --- SQL : lecture seulement, mutation = rejet immédiat ------------------------


def test_sql_read_is_retargeted_to_data_and_auto_approved():
    plan = [{"task_id": "t", "role": "files", "subtask": "SELECT count(*) FROM utilisateurs"}]
    corrected = correct_plan(plan)
    assert corrected[0]["role"] == "data"
    assert corrected[0]["auto_approve"] is True


def test_mutating_sql_is_rejected_immediately():
    for bad in (
        "DELETE FROM utilisateurs",
        "INSERT INTO logs VALUES (1)",
        "DROP TABLE utilisateurs",
        "UPDATE utilisateurs SET nom='x'",
    ):
        with pytest.raises(PlanRejected):
            correct_plan([{"task_id": "t", "role": "data", "subtask": bad}])


# --- Shell : commande whitelistée requise --------------------------------------


def test_shell_whitelisted_command_is_kept():
    plan = [{"task_id": "t", "role": "shell", "subtask": "Exécute git status pour lister les changements."}]
    assert correct_plan(plan)[0]["role"] == "shell"


def test_shell_non_whitelisted_command_is_rejected():
    with pytest.raises(PlanRejected):
        correct_plan([{
            "task_id": "t", "role": "shell",
            "subtask": "Exécute un script arbitraire de nettoyage du système.",
        }])


def test_shell_dangerous_command_is_rejected():
    with pytest.raises(PlanRejected):
        correct_plan([{"task_id": "t", "role": "shell", "subtask": "Lance rm -rf sur le projet."}])


# --- Lecture seule → auto-approve ----------------------------------------------


def test_read_only_plan_marks_auto_approve():
    plan = [
        {"task_id": "t1", "role": "ops", "subtask": "Diagnostic de l'environnement."},
        {"task_id": "t2", "role": "web", "subtask": "Recherche web sur FastAPI."},
    ]
    corrected = correct_plan(plan)
    assert note_auto_approved(corrected) == ["t1", "t2"]


def test_mutation_task_is_not_auto_approved():
    plan = [{"task_id": "t", "role": "files", "subtask": "Écris un rapport dans rapport.txt."}]
    corrected = correct_plan(plan)
    assert corrected[0].get("auto_approve") is None


# --- Prompt du planner : capacités réelles des rôles ----------------------------


def test_planner_prompt_lists_real_role_tools():
    prompt = build_planner_prompt("q", ["ops", "shell"], role_tools=role_tools())
    assert "CAPACITÉS RÉELLES DES RÔLES" in prompt
    assert "- ops : env_info, disk_usage, gpu_info" in prompt
    assert "JAMAIS « shell »" in prompt


def test_planner_prompt_without_tools_keeps_historical_shape():
    prompt = build_planner_prompt("q", ["ops"])
    assert "CAPACITÉS" not in prompt
    assert '"role": "<role>"' in prompt