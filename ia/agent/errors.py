"""Codes d'erreur de l'orchestration multi-agents.

Design (V1) : la GRANULARITÉ fine des codes existe pour la traçabilité
(logs / audit / payload), mais le comportement de contrôle de l'orchestrateur
ne pilote QUE trois états (buckets) : ``ok``, ``failed``, ``abort``.
On ne sur-spécialise PAS le comportement par code dès la V1 — chaque nouveau
code fin n'ajoute aucun chemin de contrôle à tester, il est juste porté dans
le payload et classé dans un bucket.
"""

from __future__ import annotations

from typing import Dict, Literal

# Buckets de comportement superviseur.
Bucket = Literal["ok", "failed", "abort"]

# --- Codes d'erreur (granularité fine, pour traçabilité) ---
# Plan invalide -> bucket "abort" (rien d'exécutable, on coupe).
PLAN_VALIDATION_FAILED = "PlanValidationFailed"
PLAN_EMPTY = "PlanEmpty"
PLAN_CYCLE = "PlanCycle"
TASK_DUPLICATE = "TaskDuplicate"
TASK_UNDEFINED = "TaskUndefined"
TASK_INVALID = "TaskInvalid"

# Worker en échec -> bucket "failed" (continueBroken : on signale, on synthétise).
TOOL_NOT_ALLOWED = "ToolNotAllowed"
TOOL_EXECUTION_FAILED = "ToolExecutionFailed"
LLM_TIMEOUT = "LLMTimeout"
LLM_UNREACHABLE = "LLMUnreachable"
TOKEN_BUDGET_EXCEEDED = "TokenBudgetExceeded"
MAX_ROUNDS_EXCEEDED = "MaxRoundsExceeded"

# Proposition de tool (SCRUM-99) -> bucket "failed" : le plan continue SANS le
# tool proposé (rejet au review, ou plafond de propositions par plan atteint).
TOOL_PROPOSAL_REJECTED = "ToolProposalRejected"
TOOL_PROPOSAL_LIMITED = "ToolProposalLimited"

# Superviseur / synthèse -> bucket "abort".
SYNTHESIS_FAILED = "SynthesisFailed"
SUPERVISOR_FAILED = "SupervisorFailed"

# Mapping code -> bucket. La seule source de vérité du comportement de
# contrôle. Ajouter un code fin = ajouter une ligne ici, sans toucher à la
# logique de l'orchestrateur.
CODE_BUCKET: Dict[str, Bucket] = {
    PLAN_VALIDATION_FAILED: "abort",
    PLAN_EMPTY: "abort",
    PLAN_CYCLE: "abort",
    TASK_DUPLICATE: "abort",
    TASK_UNDEFINED: "abort",
    TASK_INVALID: "abort",
    TOOL_NOT_ALLOWED: "failed",
    TOOL_EXECUTION_FAILED: "failed",
    LLM_TIMEOUT: "failed",
    LLM_UNREACHABLE: "failed",
    TOKEN_BUDGET_EXCEEDED: "failed",
    MAX_ROUNDS_EXCEEDED: "failed",
    TOOL_PROPOSAL_REJECTED: "failed",
    TOOL_PROPOSAL_LIMITED: "failed",
    SYNTHESIS_FAILED: "abort",
    SUPERVISOR_FAILED: "abort",
}


def bucket_of(error_code: str) -> Bucket:
    """Bucket de comportement d'un code d'erreur (``failed`` par défaut)."""
    return CODE_BUCKET.get(error_code, "failed")