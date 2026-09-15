"""MCP orchestration adapter over the generic multi-agent application port."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
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
        durable_state = self._prepare_durable_state(request)
        parent_task_id = durable_state.run_id
        lease_owner = f"mcp-adapter-{uuid.uuid4().hex}"
        if self._durable_store is not None:
            durable_state = self._durable_store.acquire_lease(
                parent_task_id,
                lease_owner,
            )

        def record_event(kind: str, payload: dict[str, Any]) -> None:
            nonlocal durable_state
            event = normalize_mcp_event(
                {"event": kind, **payload},
                parent_task_id=parent_task_id,
                default_phase=str(payload.get("phase") or "lead"),
                default_worker_id=payload.get("worker_id"),
            )
            if self._durable_store is not None:
                self._durable_store.append_event(parent_task_id, event)
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

        if request.resume_request_id:
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
                self._durable_store.append_event(parent_task_id, recovery_event)
            if on_event is not None:
                on_event("checkpoint_recovered", recovery_event)

        try:
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
                final_state = (
                    "partial_success"
                    if normalized.status == "partial_success"
                    else ("failed" if normalized.status == "failed" else "completed")
                )
                durable_state = self._durable_store.transition(
                    durable_state.run_id,
                    final_state,
                    phase=normalized.failure_phase or "synthesis",
                    checkpoint="completed",
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

    def _prepare_durable_state(
        self,
        request: MCPOrchestrationRequest,
    ) -> MCPDurableRunState:
        fingerprint = self._request_fingerprint(request)
        if self._durable_store is None:
            return MCPDurableRunState(
                run_id=request.resume_request_id or request.session_id,
                request_fingerprint=fingerprint,
            )
        if request.resume_request_id:
            existing = self._durable_store.get(request.resume_request_id)
            if existing is None:
                raise ValueError(f"unknown durable MCP run {request.resume_request_id!r}")
            if existing.is_terminal:
                raise ValueError(f"durable MCP run {request.resume_request_id!r} is terminal")
            if (
                existing.request_fingerprint is not None
                and existing.request_fingerprint != fingerprint
            ):
                raise ValueError(
                    f"resume context mismatch for durable MCP run {request.resume_request_id!r}"
                )
            return self._durable_store.transition(
                existing.run_id,
                "running",
                phase=existing.phase,
                checkpoint=existing.checkpoint,
                retry_count=existing.retry_count + 1,
            )
        state = self._durable_store.create(
            uuid.uuid4().hex[:12],
            request_fingerprint=fingerprint,
        )
        return self._durable_store.transition(
            state.run_id,
            "running",
            phase="lead",
            checkpoint="initialized",
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
            orchestration={
                "mode": "multi_agent",
                "event_granularity": request.event_granularity,
                "fallback": None,
                "failure_phase": failure_phase,
                "reason": reason,
                "event_policy": {
                    "granularity": request.event_granularity,
                    "hierarchy": ["lead", "worker", "synthesis"],
                },
            },
        )
