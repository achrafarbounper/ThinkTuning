"""MCP orchestration adapter over the generic multi-agent application port."""

from __future__ import annotations

import hashlib
import json
import logging
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.domain.ports import (
    MCPDurableRunState,
    MCPDurableRunStorePort,
    MCPOrchestrationRequest,
    MCPOrchestrationResult,
    MultiAgentOrchestratorPort,
    compute_mcp_failure_phase,
    compute_mcp_status,
    normalize_mcp_event,
)

logger = logging.getLogger("thinktuning.mcp.multi_agent")


@dataclass(frozen=True)
class PreparedDurableRun:
    """État durable résolu + nature du démarrage.

    ``resumed`` distingue une VRAIE reprise (run déjà engagé) d'un run
    simplement PRÉPARÉ par le transport pour exposer ``run_id`` dès
    ``orchestrate.started`` (L1 — SCRUM-152). Sans cette distinction, un run
    fraîchement créé serait compté comme une reprise (retry_count incrémenté et
    événement ``checkpoint_recovered`` fallacieux).
    """

    state: MCPDurableRunState
    resumed: bool = False


def _truncate(value: Any, limit: int = 300) -> str:
    """Tronque un résumé pour les logs (jamais de prompt complet en info)."""
    text = "" if value is None else str(value)
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _traceback_text(exc: BaseException) -> str:
    """Extrait la stacktrace la plus riche possible (rétrocompatibilité erreurs corrélées)."""
    tb: BaseException | None = exc
    parts: list[str] = []
    while tb is not None:
        tb_text = getattr(tb, "__traceback_text__", None)
        if tb_text:
            parts.append(f"[__traceback_text__] {tb_text}")
        cause = getattr(tb, "__cause__", None)
        if cause is not None:
            parts.append(f"[cause] {cause}")
        tb = cause
    if parts:
        return "\n".join(parts)
    return "".join(
        traceback.format_exception(type(exc), exc, getattr(exc, "__traceback__", None))
    ).strip()


def _format_traceback(exc: BaseException) -> dict[str, Any] | str:
    """Formate la stacktrace/current/error pour le log MCP (inclut __traceback_text__)."""
    if not hasattr(exc, "__traceback__") or exc.__traceback__ is None:
        return _traceback_text(exc)
    tb_lines = traceback.format_exc().splitlines()
    if not tb_lines or tb_lines[-1].strip() == "":
        tb_lines = tb_lines[:-1]
    return _traceback_text(exc) + "\n" + "\n".join(tb_lines)


_TRACEBACK_TRUNCATE = 400


class MultiAgentMCPAdapter:
    """Maps MCP request semantics to the transport-agnostic multi-agent port."""

    def __init__(
        self,
        orchestrator: MultiAgentOrchestratorPort,
        durable_store: MCPDurableRunStorePort | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._durable_store = durable_store

    def run(
        self,
        request: MCPOrchestrationRequest,
        *,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> MCPOrchestrationResult:
        prepared = self._prepare_durable_state(request)
        durable_state = prepared.state
        resumed = prepared.resumed
        logger.info(
            "MCP multi-agent run démarré : run_id=%s session=%s model=%s "
            "parallel=%s thinking=%s resume_run=%s approval=%s task=%s "
            "granularity=%s durable=%s",
            durable_state.run_id,
            request.session_id,
            request.model,
            request.parallel,
            request.enable_thinking,
            bool(request.run_id),
            bool(request.resume_request_id),
            request.task_id,
            request.event_granularity,
            self._durable_store is not None,
        )
        logger.debug(
            "MCP multi-agent requête : run_id=%s prompt_len=%d scope=%s "
            "prompt_preview=%r fingerprint=%s phase=%s checkpoint=%s",
            durable_state.run_id,
            len(request.prompt or ""),
            request.scope,
            _truncate(request.prompt, 120),
            durable_state.request_fingerprint,
            durable_state.phase,
            durable_state.checkpoint,
        )
        parent_task_id = durable_state.run_id
        lease_owner = f"mcp-adapter-{uuid.uuid4().hex}"
        if self._durable_store is not None:
            durable_state = self._durable_store.acquire_lease(
                parent_task_id,
                lease_owner,
            )

        def record_event(kind: str, payload: dict[str, Any]) -> None:
            nonlocal durable_state
            logger.debug(
                "MCP multi-agent event : run_id=%s kind=%s phase=%s worker=%s",
                parent_task_id,
                kind,
                payload.get("phase") or "lead",
                payload.get("worker_id") or payload.get("role") or "-",
            )
            event = normalize_mcp_event(
                {"event": kind, **payload},
                parent_task_id=parent_task_id,
                default_phase=str(payload.get("phase") or "lead"),
                default_worker_id=payload.get("worker_id"),
            )
            if self._durable_store is not None:
                # L1 (SCRUM-152) : la SÉQUENCE attribuée est renvoyée au client
                # dans le payload streamé — le front mémorise ainsi son curseur
                # de replay (``last_sequence``) sans requête supplémentaire.
                event["sequence"] = self._durable_store.append_event(parent_task_id, event)
                checkpoint = {
                    "lead": "lead_planned",
                    "worker": "workers_running",
                    "synthesis": "synthesis_running",
                }.get(str(event["phase"]))
                if checkpoint is not None:
                    durable_state = self._durable_store.transition(
                        parent_task_id,
                        "running",
                        phase=str(event["phase"]),
                        checkpoint=checkpoint,
                    )
            if on_event is not None:
                on_event(kind, event)

        if resumed:
            logger.info(
                "MCP multi-agent reprise : run_id=%s retry=%d checkpoint=%s phase=%s "
                "approval=%s task=%s last_sequence=%d",
                parent_task_id,
                durable_state.retry_count,
                durable_state.checkpoint,
                durable_state.phase,
                request.resume_request_id,
                request.task_id,
                durable_state.last_sequence,
            )
            recovery_event = normalize_mcp_event(
                {
                    "event": "checkpoint_recovered",
                    "event_id": f"checkpoint-recovered-{durable_state.retry_count}",
                    "checkpoint": durable_state.checkpoint,
                    "retry_count": durable_state.retry_count,
                    "phase": durable_state.phase,
                },
                parent_task_id=parent_task_id,
                default_phase=durable_state.phase,
            )
            if self._durable_store is not None:
                recovery_event["sequence"] = self._durable_store.append_event(
                    parent_task_id, recovery_event
                )
            if on_event is not None:
                on_event("checkpoint_recovered", recovery_event)

        try:
            logger.debug(
                "MCP multi-agent dispatch : run_id=%s streaming=%s orchestrator=%s",
                parent_task_id,
                on_event is not None,
                type(self._orchestrator).__name__,
            )
            result = (
                self._orchestrator.run_streaming(
                    request.prompt,
                    model=request.model,
                    parallel=request.parallel,
                    resume_request_id=request.resume_request_id,
                    enable_thinking=request.enable_thinking,
                    on_event=record_event,
                )
                if on_event is not None
                else self._orchestrator.run(
                    request.prompt,
                    model=request.model,
                    parallel=request.parallel,
                    resume_request_id=request.resume_request_id,
                    enable_thinking=request.enable_thinking,
                )
            )
            normalized = self._normalize(result, request)
            if self._durable_store is not None:
                for event in normalized.events:
                    self._durable_store.append_event(parent_task_id, event)
                # P0 (SCRUM-151) : un run suspendu à une validation humaine est
                # NON terminal (awaiting_approval) — il reste reprenable avec le
                # MÊME run_id après approbation. Le confondre avec "completed"
                # rendait toute reprise impossible ("run is terminal").
                if normalized.awaiting_approval:
                    final_state = "awaiting_approval"
                    final_phase = "worker"
                    final_checkpoint = "workers_running"
                else:
                    final_state = (
                        "partial_success"
                        if normalized.status == "partial_success"
                        else ("failed" if normalized.status == "failed" else "completed")
                    )
                    final_phase = normalized.failure_phase or "synthesis"
                    final_checkpoint = "completed"
                durable_state = self._durable_store.transition(
                    durable_state.run_id,
                    final_state,
                    phase=final_phase,
                    checkpoint=final_checkpoint,
                    failure_phase=normalized.failure_phase,
                    worker_errors=normalized.worker_errors,
                )
                normalized = normalized.model_copy(
                    update={
                        "run_id": durable_state.run_id,
                        "orchestration": {
                            **normalized.orchestration,
                            "durable_run": durable_state.as_snapshot(),
                            "durable_events": self._durable_store.list_events(parent_task_id),
                            "event_count": len(self._durable_store.list_events(parent_task_id)),
                        },
                    }
                )
            return normalized
        except Exception as exc:
            logger.error(
                "MCP multi-agent run échoué : run_id=%s type=%s message=%s traceback=%s",
                parent_task_id,
                type(exc).__name__,
                exc,
                _truncate(_format_traceback(exc), _TRACEBACK_TRUNCATE),
                exc_info=True,
            )
            logger.debug("MCP multi-agent run cleanup avant relance : run_id=%s", parent_task_id)
            if self._durable_store is not None:
                self._durable_store.transition(
                    durable_state.run_id,
                    "failed",
                    phase=durable_state.phase,
                    checkpoint=durable_state.checkpoint,
                    failure_phase="lead",
                    last_error=str(exc),
                )
            raise
        finally:
            if self._durable_store is not None:
                self._durable_store.release_lease(parent_task_id, lease_owner)

    def cancel(
        self,
        run_id: str,
        *,
        reason: str | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> MCPDurableRunState:
        if self._durable_store is None:
            raise RuntimeError("durable run store is required to cancel an MCP run")
        state = self._durable_store.cancel(run_id, reason=reason)
        event = normalize_mcp_event(
            {
                "event": "run_cancelled",
                "event_id": f"cancelled-{state.updated_at.isoformat()}",
                "reason": reason or "cancelled by request",
                "phase": state.phase,
            },
            parent_task_id=run_id,
            default_phase=state.phase,
        )
        if on_event is not None:
            on_event("run_cancelled", event)
        return state

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        if self._durable_store is None:
            raise RuntimeError("durable run store is required to read an MCP run")
        state = self._durable_store.get(run_id)
        if state is None:
            return None
        return {
            **state.as_snapshot(),
            "events": self._durable_store.list_events(run_id),
        }

    def get_events(self, run_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
        if self._durable_store is None:
            raise RuntimeError("durable run store is required to read MCP events")
        return self._durable_store.list_events_after(run_id, after_sequence)

    def list_runs(
        self,
        *,
        state: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if self._durable_store is None:
            raise RuntimeError("durable run store is required to list MCP runs")
        return [
            {
                **run.as_snapshot(),
                "event_count": len(self._durable_store.list_events(run.run_id)),
            }
            for run in self._durable_store.list_runs(state=state, limit=limit)
        ]

    def prepare_run(
        self,
        request: MCPOrchestrationRequest,
    ) -> dict[str, Any] | None:
        """Prépare le run durable AVANT exécution (L1 — SCRUM-152).

        Le transport SSE a besoin de ``run_id`` dès ``orchestrate.started``
        (traçabilité + annulation sur Stop) : cette méthode CRÉE le run
        (``pending``) ou retourne le run existant, sans transition ni incrément
        de reprise (contrairement à ``_prepare_durable_state``, appelée par
        ``run``). Idempotente : un ``prepare_run`` suivi de ``run`` sur le même
        ``run_id`` ne compte PAS une reprise.
        """
        if self._durable_store is None:
            return None
        fingerprint = self._request_fingerprint(request)
        if request.run_id:
            existing = self._durable_store.get(request.run_id)
            if existing is None:
                raise ValueError(f"unknown durable MCP run {request.run_id!r}")
            return existing.as_snapshot()
        state = self._durable_store.create(
            uuid.uuid4().hex[:12],
            request_fingerprint=fingerprint,
        )
        return state.as_snapshot()

    def _prepare_durable_state(
        self,
        request: MCPOrchestrationRequest,
    ) -> PreparedDurableRun:
        """Résout l'état durable du run (+ nature du démarrage).

        DÉCOUPLAGE (P0 — SCRUM-151) : la reprise durable se fait EXCLUSIVEMENT
        sur ``request.run_id``. ``request.resume_request_id`` désigne une
        demande d'approbation AgentCore (« puis-je exécuter l'action
        approuvée ? ») et n'identifie jamais un run durable. Mélanger les deux
        rendait toute approbation HITL inutilisable : le run était introuvable
        dans le store (ou pire, un run nommé d'après une demande
        d'approbation était créé).

        L1 (SCRUM-152) : un run PRÉ-CRÉÉ par ``prepare_run`` est en ``pending``
        — c'est un DÉMARRAGE, pas une reprise : aucun ``retry_count`` n'est
        consommé et aucun ``checkpoint_recovered`` n'est émis pour lui.
        """
        fingerprint = self._request_fingerprint(request)
        if self._durable_store is None:
            # Sans store durable, aucun identifiant de run n'est persisté : on
            # n'invente PAS un run_id à partir de la demande d'approbation.
            return PreparedDurableRun(
                state=MCPDurableRunState(
                    run_id=request.run_id or request.session_id,
                    request_fingerprint=fingerprint,
                ),
                resumed=False,
            )
        if request.run_id:
            existing = self._durable_store.get(request.run_id)
            if existing is None:
                raise ValueError(f"unknown durable MCP run {request.run_id!r}")
            if existing.is_terminal:
                raise ValueError(f"durable MCP run {request.run_id!r} is terminal")
            if (
                existing.request_fingerprint is not None
                and existing.request_fingerprint != fingerprint
            ):
                raise ValueError(
                    f"resume context mismatch for durable MCP run {request.run_id!r}"
                )
            resumed = existing.state != "pending"
            return PreparedDurableRun(
                state=self._durable_store.transition(
                    existing.run_id,
                    "running",
                    phase=existing.phase,
                    checkpoint=existing.checkpoint,
                    retry_count=(existing.retry_count + 1) if resumed else existing.retry_count,
                ),
                resumed=resumed,
            )
        state = self._durable_store.create(
            uuid.uuid4().hex[:12],
            request_fingerprint=fingerprint,
        )
        return PreparedDurableRun(
            state=self._durable_store.transition(
                state.run_id,
                "running",
                phase="lead",
                checkpoint="initialized",
            ),
            resumed=False,
        )

    @staticmethod
    def _request_fingerprint(request: MCPOrchestrationRequest) -> str:
        payload = {
            "prompt": request.prompt,
            "session_id": request.session_id,
            "scope": request.scope,
            "model": request.model,
            "parallel": request.parallel,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _normalize(
        result: dict[str, Any],
        request: MCPOrchestrationRequest,
    ) -> MCPOrchestrationResult:
        workers = list(result.get("workers") or [])
        errors = [
            worker
            for worker in workers
            if isinstance(worker, dict)
            and str(worker.get("status", "")).lower() in {"failed", "error"}
        ]
        lead = result.get("lead") if isinstance(result.get("lead"), dict) else None
        synthesis = result.get("synthesis") if isinstance(result.get("synthesis"), dict) else None
        failure_phase = compute_mcp_failure_phase(
            lead=lead,
            workers=workers,
            worker_errors=errors,
            synthesis=synthesis,
        )
        status = compute_mcp_status(
            lead=lead,
            workers=workers,
            worker_errors=errors,
            synthesis=synthesis,
        )
        result_status = str(result.get("status") or "").strip().lower()
        if result_status in {"success", "partial_success", "failed"}:
            status = result_status

        # --- HITL (P0 SCRUM-151) : extraction de l'approbation en attente -----
        # L'orchestrateur legacy renvoie `pending_approvals` (workers bloqués sur
        # un gate APPROVE) ; chaque worker porte `request_id` (demande
        # d'approbation) et `task_id` (reprise ciblée). `run_id` reste celui du
        # run durable — les deux identifiants ne sont JAMAIS confondus.
        awaiting_workers = [
            worker
            for worker in workers
            if isinstance(worker, dict)
            and str(worker.get("status", "")).lower() == "awaiting_approval"
        ]
        pending_approvals = [
            pending
            for pending in (result.get("pending_approvals") or [])
            if isinstance(pending, dict)
        ]
        awaiting_approval = bool(
            awaiting_workers or pending_approvals or result_status == "awaiting_approval"
        )
        approval_source = None
        if awaiting_workers:
            approval_source = awaiting_workers[0]
        elif pending_approvals:
            approval_source = pending_approvals[0]
        request_id: str | None = None
        approval: dict[str, Any] | None = None
        task_id: str | None = None
        if awaiting_approval and approval_source is not None:
            raw_request_id = (
                approval_source.get("request_id")
                or (approval_source.get("approval") or {}).get("request_id")
            )
            if raw_request_id:
                request_id = str(raw_request_id)
            raw_approval = approval_source.get("approval")
            approval = dict(raw_approval) if isinstance(raw_approval, dict) else None
            if approval is not None and request_id is not None:
                approval.setdefault("request_id", request_id)
            if approval_source.get("task_id"):
                task_id = str(approval_source["task_id"])
        if awaiting_approval:
            # Le statut MCP devient explicitement `awaiting_approval` : ni succès
            # ni échec, et le run durable correspondant reste reprenable.
            status = "awaiting_approval"
            failure_phase = None
        reason = str(result.get("reason") or result.get("phase") or "").strip() or None
        if reason in {"synthesis_timeout", "orchestration_deadline_reached"}:
            failure_phase = "synthesis"
        normalized_events = []
        for event in result.get("events") or []:
            if isinstance(event, dict):
                normalized_events.append(
                    {
                        "event": event.get("event")
                        or event.get("kind")
                        or event.get("type")
                        or "unknown",
                        **event,
                    }
                )
        if not normalized_events:
            for phase, phase_payload in {
                "lead": result.get("lead"),
                "worker": workers[0] if workers else None,
                "synthesis": synthesis,
            }.items():
                if isinstance(phase_payload, dict):
                    normalized_events.append(
                        {"event": f"orchestrate.{phase}", "phase": phase, **phase_payload}
                    )
        return MCPOrchestrationResult(
            answer=str(result.get("final_answer") or result.get("answer") or ""),
            status=status,
            failure_phase=failure_phase,
            run_id=str(result["run_id"]) if result.get("run_id") else None,
            plan=list(result.get("plan") or []),
            tasks=list(result.get("tasks") or result.get("plan") or []),
            subtasks=list(result.get("subtasks") or []),
            workers=workers,
            synthesis=synthesis,
            worker_errors=errors,
            events=[
                normalize_mcp_event(
                    event,
                    parent_task_id=str(result.get("run_id") or request.session_id),
                    default_phase="lead",
                )
                for event in normalized_events
            ],
            usage=dict(result.get("usage") or {}),
            awaiting_approval=awaiting_approval,
            request_id=request_id,
            approval=approval,
            task_id=task_id,
            orchestration={
                "mode": "multi_agent",
                "event_granularity": request.event_granularity,
                "fallback": None,
                "failure_phase": failure_phase,
                "reason": reason,
                "awaiting_approval": awaiting_approval,
                # Découplage exposé au client : deux identifiants distincts.
                "request_id": request_id,
                "run_id": str(result["run_id"]) if result.get("run_id") else None,
                "resumed": bool(request.run_id),
                "event_policy": {
                    "granularity": request.event_granularity,
                    "hierarchy": ["lead", "worker", "synthesis"],
                },
            },
        )
