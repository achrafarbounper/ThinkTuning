"""Entités agentiques du domaine : Intent, Plan, Action, Approbation.

Ces entités modélisent le flux **Intent → Plan → Action** de la couche
agentique, en ALIGNEMENT STRICT avec les structures existantes (aucune
réinvention) :

    - ``ia/agent/approvals.py``  : Decision (auto_approve / approve / reject),
      catégories d'action (read/write/delete/exec/network/system/unknown) ;
    - ``ia/agent/plan_validator.py`` : tâches {task_id, role, subtask,
      dependencies} + codes d'erreur de validation ;
    - ``core/approval_store.py`` : statuts pending/approved/rejected.

Règles :
    - Pydantic v2, immuable (frozen), sérialisation JSON stable pour l'audit
      et les stores ;
    - AUCUNE dépendance vers l'infrastructure : le domaine ne connaît ni
      SQLite, ni FastAPI, ni le client LLM.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ============================================================
# ÉNUMÉRATIONS (alignées sur le code existant)
# ============================================================


class Decision(StrEnum):
    """Décision de policy d'une action (cf. ia/agent/approvals.py)."""

    AUTO_APPROVE = "auto_approve"
    APPROVE = "approve"
    REJECT = "reject"


class ActionCategory(StrEnum):
    """Catégorie d'action, exposée au dashboard / à la trace."""

    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    EXEC = "exec"
    NETWORK = "network"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class ApprovalStatus(StrEnum):
    """Statut d'une demande d'approbation humaine (cf. core/approval_store.py)."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PlanErrorCode(StrEnum):
    """Codes d'erreur de validation de plan (cf. ia/agent/errors.py)."""

    PLAN_EMPTY = "plan_empty"
    PLAN_CYCLE = "plan_cycle"
    PLAN_VALIDATION_FAILED = "plan_validation_failed"
    TASK_INVALID = "task_invalid"
    TASK_DUPLICATE = "task_duplicate"
    TASK_UNDEFINED = "task_undefined"


def utc_now_iso() -> str:
    """Horodatage ISO 8601 UTC (millisecondes) — convention du projet."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def args_hash(args: dict[str, Any]) -> str:
    """Empreinte SHA-256 déterministe des arguments (cf. approvals._args_hash)."""
    payload = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ============================================================
# ENTITÉS
# ============================================================


class _FrozenModel(BaseModel):
    """Base : immuable, interdit les champs inconnus (fail-fast)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Intent(_FrozenModel):
    """Intention utilisateur d'un run agent, validée à l'entrée.

    Attributs :
        prompt:      demande brute de l'utilisateur ;
        session_id:  session de conversation (mémoire short-term) ;
        role:        rôle agent sollicité (cf. ia/agent/roles.py) ;
        max_rounds:  budget LLM de ce run ;
        created_at:  horodatage ISO UTC.
    """

    prompt: str = Field(min_length=1)
    session_id: str
    role: str = "default"
    max_rounds: int = Field(default=6, ge=1, le=20)
    created_at: str = Field(default_factory=utc_now_iso)


class Action(_FrozenModel):
    """Une action atomique de plan : l'appel d'un outil avec ses arguments."""

    tool: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    category: ActionCategory = ActionCategory.UNKNOWN

    def fingerprint(self) -> str:
        """Empreinte stable de l'action (arguments) pour l'audit/anti-replay."""
        return args_hash(self.args)


class PlanStep(_FrozenModel):
    """Une étape de plan — alignée sur ``{task_id, role, subtask, dependencies}``
    du validateur multi-agents, étendue d'une action outil optionnelle
    (les étapes mono-agent portent une Action, les étapes multi-agents
    portent role/subtask)."""

    task_id: str = Field(min_length=1)
    role: str = "default"
    subtask: str = ""
    dependencies: list[str] = Field(default_factory=list)
    action: Action | None = None

    @field_validator("dependencies")
    @classmethod
    def _no_self_dependency(cls, v: list[str], info) -> list[str]:
        task_id = info.data.get("task_id")
        if task_id and task_id in v:
            raise ValueError(f"La tâche {task_id!r} dépend d'elle-même (cycle trivial)")
        return v


class Plan(_FrozenModel):
    """Plan complet produit par le planner (LLM) ou le superviseur."""

    steps: list[PlanStep] = Field(default_factory=list)
    created_at: str = Field(default_factory=utc_now_iso)

    @field_validator("steps")
    @classmethod
    def _unique_task_ids(cls, v: list[PlanStep]) -> list[PlanStep]:
        ids = [s.task_id for s in v]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(f"task_id dupliqués dans le plan : {sorted(duplicates)}")
        return v

    def dependencies_undefined(self) -> list[str]:
        """Identifiants de dépendances pointant vers des tâches inexistantes."""
        known = {s.task_id for s in self.steps}
        return sorted({dep for s in self.steps for dep in s.dependencies if dep not in known})

    def has_cycle(self) -> bool:
        """Vrai si le graphe de dépendances contient un cycle (tri topo)."""
        by_id = {s.task_id: s for s in self.steps}
        state: dict[str, int] = {}  # 0 = en cours, 1 = terminé

        def dfs(task_id: str) -> bool:
            if state.get(task_id) == 1:
                return False
            if state.get(task_id) == 0:
                return True  # cycle
            state[task_id] = 0
            for dep in by_id[task_id].dependencies:
                if dep in by_id and dfs(dep):
                    return True
            state[task_id] = 1
            return False

        return any(dfs(s.task_id) for s in self.steps)

    def topological_order(self) -> list[str]:
        """Ordre d'exécution respectant les dépendances (cycle -> ValueError)."""
        if self.has_cycle():
            raise ValueError("Plan cyclique : aucun ordre topologique possible")
        ordered: list[str] = []
        done: set[str] = set()
        known = {s.task_id for s in self.steps}
        pending = list(self.steps)
        while pending:
            progressed = False
            remaining: list[PlanStep] = []
            for step in pending:
                if all(dep in done for dep in step.dependencies if dep in known):
                    ordered.append(step.task_id)
                    done.add(step.task_id)
                    progressed = True
                else:
                    remaining.append(step)
            if not progressed:  # défensif : ne devrait jamais arriver (has_cycle)
                raise ValueError("Plan non ordonnable (dépendances incohérentes)")
            pending = remaining
        return ordered


class ApprovalDecision(_FrozenModel):
    """Décision humaine (ou policy) sur une action nécessitant validation.

    Alignée sur la table ``agent_approvals`` (core/approval_store.py) :
    id, tool, args, category, reason, status, decided_by/decided_at."""

    approval_id: str
    action: Action
    status: ApprovalStatus = ApprovalStatus.PENDING
    reason: str = ""
    decided_by: str | None = None
    decided_at: str | None = None

    @property
    def is_resolved(self) -> bool:
        return self.status is not ApprovalStatus.PENDING

    @property
    def is_granted(self) -> bool:
        return self.status is ApprovalStatus.APPROVED


class PlanValidationReport(_FrozenModel):
    """Résultat de la validation déterministe d'un plan (remplace
    ``ValidationResult`` du validateur legacy, avec erreur typée)."""

    ok: bool
    steps: list[PlanStep] = Field(default_factory=list)
    error_code: PlanErrorCode | None = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Représentation JSON stable (compatibilité de la sortie du validateur)."""
        if self.ok:
            return {"ok": True, "steps": [s.model_dump(mode="json") for s in self.steps]}
        return {"ok": False, "error_code": self.error_code, "message": self.message}
