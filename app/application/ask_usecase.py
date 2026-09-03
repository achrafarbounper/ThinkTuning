"""Use-cases d'exécution d'un tour d'agent — extraits de ``api/routes/agent.py``.

``run_legacy_ask``  : boucle historique (``core.agent_cache.ask_agent_decision``).
``run_ask_core``    : noyau v2 (``app/agent/core.AgentCore``).

Les collaborators (stores, client LLM via ``decide``/``build_core``, audit,
mémoire de session) sont INJECTÉS par la route : la couche application ne
connaît ni FastAPI, ni la composition root, ni les singletons legacy. Les
routes ``api/`` résolvent les attributs de module à l'appel, ce qui préserve
les points de monkeypatch des tests existants.

Gestion d'erreur déterministe :
    - ``ValueError`` (entrée invalide côté legacy) -> propagée, run clôturé
      en ``error`` ; la route la mappe en 400 ;
    - ``AgentRunError`` (échec non récupérable du noyau v2) -> run clôturé
      en ``error`` ; la route la mappe en 502.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.agent.core import RunStatus
from app.application.run_lifecycle import (
    ACT_APPROVAL,
    ACT_RUN,
    RUN_ERROR,
    core_api_status,
    core_store_status,
    core_tool_events,
    create_approval_request,
    legacy_run_status,
    make_approval_gateway,
    resolve_resume_hash,
)
from app.domain.entities.plan import Intent
from app.domain.errors import AgentRunError


@dataclass
class AskOutcome:
    """Issue d'un tour d'agent (legacy ou v2), telle que consommée par l'API."""

    response: str
    status: str                     # completed / awaiting_approval / rejected / error
    request_id: str | None
    approval: dict | None
    run_id: str
    model: str


@dataclass
class AskCoreResult:
    """Issue du noyau v2 : réponse + compteurs pour l'audit."""

    answer: str
    api_status: str
    request_id: str
    approval: dict | None
    run_id: str
    model: str
    result: Any = field(default=None, repr=False)   # RunResult brut (streaming)


def run_legacy_ask(
    *,
    prompt: str,
    session_id: str | None,
    resume_request_id: str | None,
    model: str,
    run_store: Any,
    decide: Callable[..., dict],
    load_history: Callable[[str | None, str | None], list[dict]],
    persist_exchange: Callable[..., None],
    audit_log: Callable[..., Any],
) -> AskOutcome:
    """Un tour d'agent via la boucle historique (endpoint ``POST /api/agent/ask``).

    ``decide`` est ``core.agent_cache.ask_agent_decision`` (pont du gate de
    décision auto_approve / approve / reject). Les erreurs réseau y sont déjà
    traduites (Timeout -> HTTPException 504, ConnectionError -> 502).
    """
    run_row = run_store.start_run(prompt, model=model, source="ask")
    audit_log(
        ACT_RUN,
        subject="ask",
        detail={"status": "started", "model": model},
        run_id=run_row["id"],
    )
    try:
        decision = decide(
            prompt,
            resume_request_id=resume_request_id,
            # Mémoire de session : rejoue les tours précédents en contexte pour
            # que l'agent se souvienne de la conversation (ex. le nom).
            history_messages=load_history(session_id, resume_request_id),
        )
    except ValueError as exc:
        run_store.finish_run(run_row["id"], RUN_ERROR, error=str(exc))
        raise

    status = legacy_run_status(decision.get("status", "completed"))
    run_store.finish_run(
        run_row["id"],
        status,
        answer_summary=(decision.get("response") or "")[:300],
    )
    audit_log(
        ACT_RUN,
        subject="ask",
        detail={
            "status": decision.get("status", "completed"),
            "model": model,
        },
        run_id=run_row["id"],
    )
    persist_exchange(session_id, prompt, decision.get("response") or "")

    return AskOutcome(
        response=decision["response"] or "",
        status=decision.get("status", "completed"),
        request_id=decision.get("request_id"),
        approval=decision.get("approval"),
        run_id=run_row["id"],
        model=model,
    )


def run_ask_core(
    *,
    prompt: str,
    session_id: str | None,
    resume_request_id: str | None,
    model: str,
    run_store: Any,
    approval_store: Any,
    build_core: Callable[..., Any],
    load_history: Callable[[str | None, str | None], list[dict]],
    persist_exchange: Callable[..., None],
    audit_log: Callable[..., Any],
) -> AskCoreResult:
    """Un tour d'agent via le noyau v2 (endpoint ``POST /api/agent/ask/core``).

    Orchestration : ouverture du run + audit, gateway de reprise par empreinte,
    exécution ``Intent -> Plan -> Policy -> Budget -> Action``, création de la
    demande d'approbation le cas échéant, clôture du run, persistance de
    l'échange. Lève ``AgentRunError`` après clôture en ``error`` si le noyau
    échoue (la route la traduit en 502).
    """
    run_row = run_store.start_run(prompt, model=model, source="ask_core")
    audit_log(ACT_RUN, subject="ask_core",
              detail={"status": "started"}, run_id=run_row["id"])

    # Gateway d'approbation : si le run reprend après validation humaine
    # (resume_request_id -> action approuvée), la gateway accorde UNIQUEMENT
    # l'action dont l'empreinte (args_hash) correspond à la demande approuvée.
    resume_hash = resolve_resume_hash(approval_store, resume_request_id)
    approval_gateway = make_approval_gateway(resume_hash)

    try:
        core = build_core(approval_gateway=approval_gateway)
        history = load_history(session_id, resume_request_id)
        result = core.run(
            Intent(prompt=prompt, session_id=session_id or "default"),
            history=history,
        )
    except Exception as exc:
        run_store.finish_run(run_row["id"], RUN_ERROR, error=str(exc))
        raise AgentRunError(str(exc)) from exc

    approval_payload: dict | None = None
    if result.status is RunStatus.PENDING_APPROVAL and result.awaiting_action:
        action = result.awaiting_action
        approval_payload = create_approval_request(approval_store, action, prompt)
        audit_log(
            ACT_APPROVAL, subject="ask_core",
            detail={"request_id": approval_payload["request_id"], "tool": action.tool},
            run_id=run_row["id"],
        )

    api_status = core_api_status(result.status)
    run_store.finish_run(
        run_row["id"], core_store_status(result.status),
        answer_summary=(result.answer or "")[:300],
    )
    audit_log(
        ACT_RUN, subject="ask_core",
        detail={"status": api_status, "actions": len(result.actions),
                "rounds": result.rounds_used, "tool_calls": result.tool_calls_used},
        run_id=run_row["id"],
    )
    if api_status != "error":
        persist_exchange(session_id, prompt, result.answer or "",
                         tool_events=core_tool_events(result) or None)

    return AskCoreResult(
        answer=result.answer or "",
        api_status=api_status,
        request_id=approval_payload["request_id"] if approval_payload else run_row["id"],
        approval=approval_payload,
        run_id=run_row["id"],
        model=model,
        result=result,
    )
