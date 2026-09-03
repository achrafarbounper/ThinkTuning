"""Cycle de vie d'un run agent — helpers métier partagés par les use-cases.

Regroupe ce qui était dupliqué entre ``/ask``, ``/ask/core``, ``/ask/stream``,
``/ask/core/stream`` et le canal WebSocket de ``api/routes/agent.py`` :

    - mapping des statuts de décision legacy -> statuts du run_store ;
    - mapping des statuts ``RunStatus`` du noyau v2 -> statut API / run_store ;
    - résolution du hash de reprise (empreinte SHA-256 de l'action approuvée) ;
    - gateway d'approbation par empreinte ;
    - création d'une demande d'approbation + payload IHM ;
    - traduction des traces du noyau en événements d'outils stockables.

Note de migration : les chaînes d'action d'audit sont figées localement (même
valeur que ``core/audit_store.py`` — ``agent_run`` / ``approval``) pour éviter
un import legacy depuis la couche application ; un test de contrat peut
verrouiller l'alignement (convention ``test_*_match_legacy``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.agent.core import RunStatus
from core.approval_store import APPROVED
from core.run_store import (
    AWAITING_APPROVAL as RUN_AWAITING_APPROVAL,
)
from core.run_store import (
    COMPLETED as RUN_COMPLETED,
)
from core.run_store import (
    ERROR as RUN_ERROR,
)
from core.run_store import (
    REJECTED as RUN_REJECTED,
)

# Actions d'audit (alignées sur core/audit_store.py — voir docstring).
ACT_RUN = "agent_run"
ACT_APPROVAL = "approval"

# Statuts de décision legacy (gate ask_agent_decision) -> statuts du run_store.
DECISION_RUN_STATUS: dict[str, str] = {
    "completed": RUN_COMPLETED,
    "awaiting_approval": RUN_AWAITING_APPROVAL,
    "rejected": RUN_REJECTED,
}

# Statuts du noyau v2 -> statut API (« completed » par défaut).
RUN_STATUS_TO_API: dict[RunStatus, str] = {
    RunStatus.COMPLETED: "completed",
    RunStatus.PENDING_APPROVAL: "awaiting_approval",
    RunStatus.REJECTED_LOOP: "rejected",
    RunStatus.BUDGET_EXHAUSTED: "error",
    RunStatus.FAILED: "error",
}

# Statuts du noyau v2 -> statuts persistés du run_store.
RUN_STATUS_TO_STORE: dict[RunStatus, str] = {
    RunStatus.COMPLETED: RUN_COMPLETED,
    RunStatus.PENDING_APPROVAL: RUN_AWAITING_APPROVAL,
    RunStatus.REJECTED_LOOP: RUN_REJECTED,
    RunStatus.BUDGET_EXHAUSTED: RUN_ERROR,
    RunStatus.FAILED: RUN_ERROR,
}


def legacy_run_status(decision_status: Any) -> str:
    """Statut de run_store pour un statut de décision legacy (défaut : completed)."""
    return DECISION_RUN_STATUS.get(str(decision_status or "completed"), RUN_COMPLETED)


def core_api_status(status: RunStatus) -> str:
    """Statut API (AskResponse.status) pour un statut ``RunStatus`` du noyau."""
    return RUN_STATUS_TO_API.get(status, "completed")


def core_store_status(status: RunStatus) -> str:
    """Statut persisté (run_store) pour un statut ``RunStatus`` du noyau."""
    return RUN_STATUS_TO_STORE.get(status, RUN_COMPLETED)


def resolve_resume_hash(approval_store, resume_request_id: str | None) -> str | None:
    """Hash (empreinte) de l'action approuvée correspondant à une reprise.

    Si le run reprend après validation humaine (``resume_request_id`` ->
    demande ``approved``), la gateway n'accordera QUE l'action dont
    l'empreinte SHA-256 des arguments correspond exactement.
    """
    if not resume_request_id:
        return None
    resume_record = approval_store.get(resume_request_id)
    if resume_record and resume_record.get("status") == APPROVED:
        return resume_record.get("args_hash")
    return None


def make_approval_gateway(resume_hash: str | None) -> Callable[[Any], bool]:
    """Gateway ``(Action) -> bool`` : n'accorde que l'action dont l'empreinte
    correspond au hash de reprise (aucune autre action ne passe)."""
    def _approval_gateway(action) -> bool:
        return bool(resume_hash and action.fingerprint() == resume_hash)

    return _approval_gateway


def create_approval_request(approval_store: Any, action: Any, prompt: str) -> dict:
    """Persiste une demande d'approbation pour l'action en attente et renvoie
    le payload IHM ``{"request_id", "tool", "args"}``."""
    record = approval_store.create(
        action.tool,
        action.args,
        action.category.value,
        "approve",
        "Policy : validation humaine requise",
        prompt=prompt,
        args_hash=action.fingerprint(),
    )
    return {
        "request_id": record.get("request_id") or record.get("id"),
        "tool": action.tool,
        "args": action.args,
    }


def core_tool_events(result: Any) -> list[dict]:
    """Traduit les traces d'actions du noyau en événements d'outils stockables.

    Même contrat que les frames SSE « core_tool » (tool_start / tool_result) :
    l'IHM rejoue ces paires au rechargement d'une session pour réafficher les
    outils exécutés dans le chat (cf. mapStoredToolCalls côté front).
    """
    events: list[dict] = []
    for trace in getattr(result, "actions", []) or []:
        if trace.status not in ("done", "error"):
            continue  # awaiting_approval / rejected : aucun outil exécuté
        events.append({"event": "tool_start", "tool": trace.tool, "args": trace.args})
        events.append({
            "event": "tool_result", "tool": trace.tool,
            "status": "ok" if trace.status == "done" else "error",
            "summary": trace.result_summary or trace.error,
        })
    return events
