"""Ports MCP — contrats du protocole Model Context Protocol (S1, tâche 3).

Formalise l'interface entre la couche serveur MCP (``app/infrastructure/mcp``)
et le domaine. Le serveur projette ces ports en protocoles JSON-RPC 2.0 :

    tools/list  + tools/call       → MCPToolRegistryPort
    resources/list  + resources/read → MCPResourceRegistryPort
    prompts/list  + prompts/get    → MCPPromptRegistryPort
    sampling/create                → SamplingPort

Chaque port est un Protocol ``runtime_checkable`` : les implémentations
infrastructure (``InMemoryToolProvider``, futur adapter legacy S2, LLM client)
peuvent être vérifiées par ``isinstance`` — fail-fast sur les contrats brisés
(``tests/test_mcp_ports_contract.py``).

Règles d'or :
    - le domaine ne connaît AUCUN transport (SSE, stdio) ni framework (FastAPI) ;
    - le filtrage par scope de sécurité relève de l'infrastructure
      (``MCPServer._visible_tools``), pas du port : le port rend la vérité ;
    - les erreurs métier (404) sont des ``DomainError`` (cf. app/domain/errors.py) ;
    - l'isollement d'erreurs (catch-all autour des handlers) est de la
      responsabilité de l'implémentation, pas du port.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.agent.policies.budget import BudgetPolicy
from app.domain.entities.mcp import (
    MCPPromptMessage,
    MCPPromptTemplate,
    MCPResource,
    MCPTool,
    SamplingRequest,
    SamplingResponse,
)
from app.domain.ports.ports import Message

__all__ = [
    "BudgetPolicy",
    "ExecutionContext",
    "MCPDurableRunState",
    "MCPDurableRunStorePort",
    "MCPOrchestrationPort",
    "MCPOrchestrationRequest",
    "MCPOrchestrationResult",
    "compute_mcp_failure_phase",
    "compute_mcp_status",
    "normalize_mcp_event",
    "normalize_mcp_event_granularity",
    "MCPResourceRegistryPort",
    "MCPPromptRegistryPort",
    "MCPSecurityScope",
    "MCPToolRegistryPort",
    "SamplingPort",
    "SamplingRequest",
    "SamplingResponse",
    "WorkerScopePolicy",
    "_sampling_create_text",
    "MCPHostPort",
    "MCPHostTool",
    "MCPRemoteCall",
]


@dataclass(frozen=True)
class WorkerScopePolicy:
    """Scope restrictions enforced for a worker spawned by the lead."""

    parent_scope: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()
    max_tools_per_worker: int = 10

    def validate(self, context: ExecutionContext, *, worker_id: str = "worker") -> None:
        """Valide le scope EFFECTIF d'un worker (jamais le scope parent).

        Les trois contraintes sont réellement appliquées : appartenance au
        scope parent, absence d'outil interdit, plafond d'outils par worker.
        Une levée ``ValueError`` est traduite par le transport MCP en repli
        explicite ``worker_scope_violation`` (jamais en exécution silencieuse).
        """
        if not set(context.allowed_scopes).issubset(set(self.parent_scope)):
            raise ValueError(f"worker {worker_id} exceeds parent scope")
        if self.max_tools_per_worker < 1:
            raise ValueError("max_tools_per_worker must be >= 1")
        forbidden = sorted(set(self.forbidden_tools) & set(context.allowed_tools))
        if forbidden:
            raise ValueError(f"worker {worker_id} requests forbidden tools: {forbidden}")
        if len(context.allowed_tools) > self.max_tools_per_worker:
            raise ValueError(
                f"worker {worker_id} exceeds max_tools_per_worker "
                f"({len(context.allowed_tools)} > {self.max_tools_per_worker})"
            )


@dataclass(frozen=True)
class ExecutionContext:
    """Shared runtime policy used by both HTTP and MCP surfaces."""

    user_id: str = "system"
    tenant_id: str = "default"
    allowed_tools: tuple[str, ...] = ()
    allowed_resources: tuple[str, ...] = ()
    allowed_scopes: tuple[str, ...] = ()
    budget: BudgetPolicy = field(default_factory=BudgetPolicy)
    approvals: tuple[str, ...] = ()

    def for_worker(
        self,
        worker_id: str,
        *,
        allowed_tools: Iterable[str] | None = None,
        allowed_resources: Iterable[str] | None = None,
        allowed_scopes: Iterable[str] | None = None,
    ) -> ExecutionContext:
        effective_tools = tuple(allowed_tools or self.allowed_tools)
        effective_resources = tuple(allowed_resources or self.allowed_resources)
        effective_scopes = tuple(allowed_scopes or self.allowed_scopes)
        if not set(effective_tools).issubset(set(self.allowed_tools)):
            raise ValueError(f"worker {worker_id} tried to widen allowed tools")
        if not set(effective_resources).issubset(set(self.allowed_resources)):
            raise ValueError(f"worker {worker_id} tried to widen allowed resources")
        if not set(effective_scopes).issubset(set(self.allowed_scopes)):
            raise ValueError(f"worker {worker_id} tried to widen allowed scopes")
        return ExecutionContext(
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            allowed_tools=effective_tools,
            allowed_resources=effective_resources,
            allowed_scopes=effective_scopes,
            budget=self.budget,
            approvals=self.approvals,
        )


class MCPOrchestrationRequest(BaseModel):
    """Validated MCP request passed to the orchestration application port."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = Field(min_length=1)
    session_id: str = "default"
    scope: str = "default"
    model: str | None = None
    parallel: bool = False
    enable_thinking: bool = False
    event_granularity: str = "summary"
    # P0 (SCRUM-151) — DÉCOUPLAGE des deux identifiants :
    #   * ``run_id``           : identifiant DURABLE du run multi-agent (store
    #     durable, replay, lease, reprise ciblée). Il est stable d'une reprise
    #     à l'autre et ne dépend JAMAIS de la validation humaine ;
    #   * ``resume_request_id`` : identifiant de la DEMANDE D'APPROBATION
    #     AgentCore (table ``approvals``) — il autorise l'exécution de l'action
    #     approuvée (empreinte SHA-256) et n'est PAS un identifiant de run.
    run_id: str | None = None
    resume_request_id: str | None = None
    # Reprise CIBLÉE : sous-tâche (worker) bloquée sur l'approbation. Purement
    # déclaratif côté MCP (l'orchestrateur re-dispatch déjà le seul worker
    # dont le ``request_id`` est repris) — tracé pour l'audit et le Flow Map.
    task_id: str | None = None

    @classmethod
    def from_values(cls, **values: Any) -> MCPOrchestrationRequest:
        values["prompt"] = str(values.get("prompt") or "").strip()
        values["session_id"] = str(values.get("session_id") or "default").strip() or "default"
        values["scope"] = str(values.get("scope") or "default").strip() or "default"
        event_granularity = str(values.get("event_granularity") or "summary").strip().lower()
        values["event_granularity"] = event_granularity
        allowed = {"minimal", "summary", "verbose"}
        if values.get("event_granularity") not in allowed:
            raise ValueError("event_granularity doit être « minimal », « summary » ou « verbose »")
        # Identifiants optionnels : ``None``/"" → None (jamais la chaîne vide,
        # qui créerait un run durable nommé "").
        for key in ("run_id", "resume_request_id", "task_id"):
            raw = values.get(key)
            normalized = str(raw).strip() if raw is not None else ""
            values[key] = normalized or None
        return cls(**values)


VALID_MCP_ORCHESTRATION_STATUSES = {
    "success",
    "partial_success",
    "failed",
    # P0 (SCRUM-151) : un run interrompu par une validation humaine n'est ni
    # un succès ni un échec — le statut est additif et non terminal.
    "awaiting_approval",
}
VALID_MCP_FAILURE_PHASES = {"lead", "worker", "synthesis"}
VALID_MCP_EVENT_GRANULARITIES = {"minimal", "summary", "verbose"}
# Phases canoniques d'un événement durable/streamé (hiérarchie lead → worker
# → synthesis). ``orchestration`` reste accepté (événements de haut niveau).
_VALID_MCP_EVENT_PHASES = {"lead", "worker", "synthesis", "orchestration"}
VALID_MCP_RUN_STATES = {
    "pending",
    "running",
    # P0 (SCRUM-151) : run durable NON terminal suspendu à une validation
    # humaine — la reprise ciblée repart de cet état avec le même `run_id`.
    "awaiting_approval",
    "partial_success",
    "completed",
    "failed",
    "cancelled",
}
VALID_MCP_RUN_CHECKPOINTS = {
    "initialized",
    "lead_planned",
    "workers_running",
    "synthesis_running",
    "completed",
}
# L1 (SCRUM-152) : ordre TOTAL des checkpoints — un checkpoint ne régresse
# JAMAIS (les événements d'un worker peuvent arriver après un événement de
# synthèse sur le chemin SSE ; repartir de ``lead_planned`` rejouerait des
# phases déjà acquittées côté reprise).
_MCP_CHECKPOINT_RANK = {
    "initialized": 0,
    "lead_planned": 1,
    "workers_running": 2,
    "synthesis_running": 3,
    "completed": 4,
}
_MCP_RUN_TRANSITIONS = {
    "pending": {"running", "cancelled"},
    "running": {
        "running",
        "awaiting_approval",
        "partial_success",
        "completed",
        "failed",
        "cancelled",
    },
    # Resume ciblé : awaiting_approval -> running (même run_id, retry_count +1).
    "awaiting_approval": {
        "running",
        "awaiting_approval",
        "partial_success",
        "completed",
        "failed",
        "cancelled",
    },
    "partial_success": {"running", "completed", "failed", "cancelled"},
    "completed": set(),
    "failed": set(),
    "cancelled": set(),
}


@dataclass(frozen=True)
class MCPDurableRunState:
    """Lifecycle state machine for multi-agent MCP runs.

    This is intentionally a small, explicit model: it gives the durable-run
    design a stable state boundary without forcing the protocol-level result to
    expose more statuses than the user-facing MCP contract.
    """

    run_id: str = "default"
    request_fingerprint: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    state: str = "pending"
    phase: str = "lead"
    checkpoint: str = "initialized"
    failure_phase: str | None = None
    worker_errors: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    retry_count: int = 0
    version: int = 0
    # L1 (SCRUM-152) : dernière séquence d'événement MÉMORISÉE par le run —
    # un client coupé reprend le flux avec ``after_sequence=last_sequence``
    # sans rejouer tout l'historique (replay incrémental).
    last_sequence: int = 0
    last_error: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def transition(
        self,
        new_state: str,
        *,
        phase: str | None = None,
        checkpoint: str | None = None,
        failure_phase: str | None = None,
        worker_errors: Iterable[dict[str, Any]] | None = None,
        retry_count: int | None = None,
        last_sequence: int | None = None,
        last_error: str | None = None,
    ) -> MCPDurableRunState:
        """Applique une transition d'état VALIDÉE par la machine à états.

        L1 (SCRUM-152) : le ``checkpoint`` est MONOTONE — une transition ne
        peut jamais revenir en arrière (un événement worker tardif ne doit pas
        réécrire ``synthesis_running`` en ``workers_running``). Le
        ``last_sequence`` suit la même règle (max des séquences observées) pour
        que le replay reprenne exactement après le dernier événement émis.
        """
        normalized_state = str(new_state or "").strip().lower()
        if normalized_state not in VALID_MCP_RUN_STATES:
            raise ValueError(f"state must be one of {sorted(VALID_MCP_RUN_STATES)}")
        if normalized_state not in _MCP_RUN_TRANSITIONS.get(self.state, set()):
            raise ValueError(
                f"invalid lifecycle transition from {self.state!r} to {normalized_state!r}"
            )
        next_phase = str(phase or self.phase).strip().lower() if phase is not None else self.phase
        candidate_checkpoint = (
            str(checkpoint or self.checkpoint).strip().lower()
            if checkpoint is not None
            else self.checkpoint
        )
        if candidate_checkpoint not in VALID_MCP_RUN_CHECKPOINTS:
            raise ValueError(f"checkpoint must be one of {sorted(VALID_MCP_RUN_CHECKPOINTS)}")
        # MONOTONIE : on conserve le checkpoint le plus AVANCÉ des deux. Un
        # événement tardif (worker après synthèse) ne peut donc pas faire
        # régresser la progression durable ni provoquer un rejeu de phase.
        next_checkpoint = max(
            (self.checkpoint, candidate_checkpoint),
            key=lambda value: _MCP_CHECKPOINT_RANK.get(value, 0),
        )
        next_failure_phase: str | None
        if failure_phase is not None:
            normalized_failure = str(failure_phase).strip().lower()
            if normalized_failure not in VALID_MCP_FAILURE_PHASES:
                raise ValueError(f"failure_phase must be one of {sorted(VALID_MCP_FAILURE_PHASES)}")
            next_failure_phase = normalized_failure
        else:
            next_failure_phase = self.failure_phase

        next_errors = tuple(worker_errors) if worker_errors is not None else self.worker_errors
        next_retry_count = int(retry_count) if retry_count is not None else self.retry_count
        next_last_error = last_error if last_error is not None else self.last_error
        # MONOTONIE du curseur de replay : la séquence ne recule jamais.
        next_last_sequence = max(int(self.last_sequence), int(last_sequence or 0))
        if normalized_state in {"failed", "partial_success", "completed", "cancelled"}:
            next_phase = next_phase or "lead"
        return MCPDurableRunState(
            run_id=self.run_id,
            request_fingerprint=self.request_fingerprint,
            lease_owner=self.lease_owner,
            lease_expires_at=self.lease_expires_at,
            state=normalized_state,
            phase=next_phase,
            checkpoint=next_checkpoint,
            failure_phase=next_failure_phase,
            worker_errors=next_errors,
            retry_count=next_retry_count,
            version=self.version + 1,
            last_sequence=next_last_sequence,
            last_error=next_last_error,
            created_at=self.created_at,
            updated_at=datetime.now(UTC),
        )

    @property
    def final_status(self) -> str:
        if self.state == "failed":
            return "failed"
        if self.state == "partial_success":
            return "partial_success"
        if self.state == "awaiting_approval":
            # Non terminal : ni succès ni échec (validation humaine en attente).
            return "awaiting_approval"
        if self.state == "completed":
            return "success"
        return "success" if self.state in {"pending", "running"} else "success"

    @property
    def is_terminal(self) -> bool:
        return self.state in {"completed", "failed", "cancelled"}

    @property
    def is_resumable(self) -> bool:
        """Un run suspendu à une validation humaine reste reprenable."""
        return self.state == "awaiting_approval"

    def as_snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "request_fingerprint": self.request_fingerprint,
            "lease_owner": self.lease_owner,
            "lease_expires_at": (
                self.lease_expires_at.isoformat() if self.lease_expires_at else None
            ),
            "state": self.state,
            "phase": self.phase,
            "checkpoint": self.checkpoint,
            "failure_phase": self.failure_phase,
            "worker_errors": list(self.worker_errors),
            "retry_count": self.retry_count,
            "version": self.version,
            "last_sequence": self.last_sequence,
            "last_error": self.last_error,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@runtime_checkable
class MCPDurableRunStorePort(Protocol):
    """Persistence boundary for resumable MCP orchestration runs."""

    def create(
        self,
        run_id: str,
        *,
        request_fingerprint: str | None = None,
    ) -> MCPDurableRunState: ...

    def get(self, run_id: str) -> MCPDurableRunState | None: ...

    def append_event(self, run_id: str, event: dict[str, Any]) -> int:
        """Persiste un événement et retourne sa SÉQUENCE (curseur de replay).

        L1 (SCRUM-152) : la séquence retournée est mémorisée sur le run
        (``last_sequence``) et renvoyée au client dans l'événement streamé —
        un flux coupé reprend donc avec ``after_sequence=last_sequence`` sans
        rejouer l'historique complet. Idempotent sur ``event_id``.
        """
        ...

    def list_events(self, run_id: str) -> list[dict[str, Any]]: ...

    def list_events_after(self, run_id: str, after_sequence: int = 0) -> list[dict[str, Any]]: ...

    def last_sequence(self, run_id: str) -> int:
        """Dernière séquence persistée du run (0 si aucun événement)."""
        ...

    def list_runs(
        self,
        *,
        state: str | None = None,
        limit: int = 50,
    ) -> list[MCPDurableRunState]: ...

    def cancel(self, run_id: str, *, reason: str | None = None) -> MCPDurableRunState: ...

    def acquire_lease(
        self,
        run_id: str,
        owner: str,
        *,
        ttl_seconds: int = 60,
    ) -> MCPDurableRunState: ...

    def release_lease(self, run_id: str, owner: str) -> MCPDurableRunState: ...

    def renew_lease(
        self,
        run_id: str,
        owner: str,
        *,
        ttl_seconds: int = 60,
    ) -> MCPDurableRunState: ...

    def transition(
        self,
        run_id: str,
        new_state: str,
        *,
        phase: str | None = None,
        checkpoint: str | None = None,
        failure_phase: str | None = None,
        worker_errors: Iterable[dict[str, Any]] | None = None,
        retry_count: int | None = None,
        last_sequence: int | None = None,
        last_error: str | None = None,
    ) -> MCPDurableRunState: ...


def normalize_mcp_event_granularity(value: Any) -> str:
    """Canonicalize the MCP event granularity policy."""
    normalized = str(value or "summary").strip().lower()
    if normalized not in VALID_MCP_EVENT_GRANULARITIES:
        raise ValueError(
            f"event_granularity must be one of {sorted(VALID_MCP_EVENT_GRANULARITIES)}"
        )
    return normalized


def normalize_mcp_event(
    event: dict[str, Any] | None,
    *,
    parent_task_id: str | None = None,
    default_phase: str = "lead",
    default_worker_id: str | None = None,
) -> dict[str, Any]:
    """Ensure every emitted MCP event carries stable hierarchy metadata.

    L1 (SCRUM-152) — normalisation RÉPARÉE de ``phase`` / ``worker_id`` :

      * un événement porteur d'un ``worker_id`` est TOUJOURS classé ``worker`` :
        auparavant, un événement worker sans ``phase`` explicite retombait sur
        ``default_phase`` (``lead``), ce qui faisait disparaître le worker de la
        hiérarchie durable et du Flow Map ;
      * un ``phase`` inconnu (``dispatch``, ``phase-1``…) n'est plus écrasé
        silencieusement par le défaut : il est dérivé du nom d'événement ;
      * ``worker_id`` est normalisé (chaîne nettoyée, ``None`` si vide) sans
        jamais écraser une valeur explicite.
    """
    payload = dict(event or {})
    if "event" not in payload:
        for key in ("kind", "type", "name"):
            if key in payload:
                payload["event"] = str(payload[key])
                break
    payload.setdefault("event_id", f"mcp-{uuid.uuid4().hex}")
    payload.setdefault("timestamp", datetime.now(UTC).isoformat())
    payload.setdefault("parent_task_id", parent_task_id)

    # --- worker_id : explicite > défaut ; jamais la chaîne vide -------------
    raw_worker = payload.get("worker_id", default_worker_id)
    worker_id = str(raw_worker).strip() if raw_worker is not None else ""
    payload["worker_id"] = worker_id or None

    # --- phase : explicite valide > dérivation (worker/synthèse) > défaut ---
    raw_phase = str(payload.get("phase") or "").strip().lower()
    event_name = str(payload.get("event") or "").strip().lower()
    if raw_phase in _VALID_MCP_EVENT_PHASES:
        phase = raw_phase
    elif payload.get("worker_id") is not None:
        phase = "worker"
    elif event_name.startswith(("orchestrate.worker", "agent.worker", "mcp.orchestrate.worker")):
        phase = "worker"
    elif event_name.startswith(
        ("orchestrate.synthesis", "agent.synthesis", "mcp.orchestrate.synthesis")
    ):
        phase = "synthesis"
    else:
        normalized_default = str(default_phase or "lead").strip().lower()
        phase = normalized_default if normalized_default in _VALID_MCP_EVENT_PHASES else "lead"
    payload["phase"] = phase
    return payload


def _normalize_phase_status(value: Any) -> str:
    return str(value or "").strip().lower()


def compute_mcp_failure_phase(
    *,
    lead: dict[str, Any] | None = None,
    workers: Iterable[dict[str, Any]] | None = None,
    worker_errors: Iterable[dict[str, Any]] | None = None,
    synthesis: dict[str, Any] | None = None,
) -> str | None:
    """Identify which phase failed when the global result is not successful."""
    lead_status = _normalize_phase_status((lead or {}).get("status"))
    if lead_status in {"failed", "error", "cancelled"}:
        return "lead"

    worker_list = list(workers or ())
    failed_workers = [
        worker
        for worker in worker_list
        if _normalize_phase_status(worker.get("status")) in {"failed", "error", "cancelled"}
    ]
    if failed_workers or list(worker_errors or ()):
        return "worker"

    synthesis_status = _normalize_phase_status((synthesis or {}).get("status"))
    if synthesis_status in {"failed", "error", "cancelled"}:
        return "synthesis"
    return None


def compute_mcp_status(
    *,
    lead: dict[str, Any] | None = None,
    workers: Iterable[dict[str, Any]] | None = None,
    worker_errors: Iterable[dict[str, Any]] | None = None,
    synthesis: dict[str, Any] | None = None,
    fallback: bool = False,
    status: str | None = None,
) -> str:
    """Return the normalized global status for an MCP multi-agent run.

    Canonical status contract: success | partial_success | failed.
    Specific failure causes are exposed separately via ``failure_phase`` so the
    client can distinguish lead, worker, or synthesis failures without breaking
    the legacy status vocabulary.
    """
    if status is not None:
        normalized = str(status).strip().lower()
        if normalized in VALID_MCP_ORCHESTRATION_STATUSES:
            return normalized
        raise ValueError(f"status must be one of {sorted(VALID_MCP_ORCHESTRATION_STATUSES)}")

    failure_phase = compute_mcp_failure_phase(
        lead=lead,
        workers=workers,
        worker_errors=worker_errors,
        synthesis=synthesis,
    )
    if failure_phase == "lead":
        return "failed"
    if failure_phase == "synthesis":
        return "failed"
    if failure_phase == "worker":
        return "partial_success"
    if fallback:
        return "success"
    return "success"


class MCPOrchestrationResult(BaseModel):
    """Stable, additive result contract for MCP multi-agent orchestration."""

    model_config = ConfigDict(frozen=True, extra="allow")

    answer: str = ""
    status: str = "success"
    failure_phase: str | None = None
    run_id: str | None = None
    plan: list[dict[str, Any]] = Field(default_factory=list)
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    subtasks: list[dict[str, Any]] = Field(default_factory=list)
    workers: list[dict[str, Any]] = Field(default_factory=list)
    synthesis: dict[str, Any] | None = None
    worker_errors: list[dict[str, Any]] = Field(default_factory=list)
    events: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    orchestration: dict[str, Any] = Field(default_factory=dict)
    # --- HITL (P0 SCRUM-151) -------------------------------------------------
    # `run_id` (durable, repris à l'identique) et `request_id` (demande
    # d'approbation) sont DEUX identifiants distincts : le client affiche la
    # carte de validation avec `request_id`, puis relance AVEC `run_id`.
    awaiting_approval: bool = False
    request_id: str | None = None
    approval: dict[str, Any] | None = None
    # Reprise ciblée : sous-tâche bloquée sur l'approbation (si connue).
    task_id: str | None = None

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        normalized = str(value).strip().lower()
        if normalized not in VALID_MCP_ORCHESTRATION_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_MCP_ORCHESTRATION_STATUSES)}")
        return normalized

    @field_validator("failure_phase")
    @classmethod
    def validate_failure_phase(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip().lower()
        if normalized not in VALID_MCP_FAILURE_PHASES:
            raise ValueError(f"failure_phase must be one of {sorted(VALID_MCP_FAILURE_PHASES)}")
        return normalized


@runtime_checkable
class MCPOrchestrationPort(Protocol):
    """MCP-specific orchestration boundary.

    MCP owns request/result/event semantics here; the generic multi-agent
    coordinator remains behind the application adapter.
    """

    def run(
        self,
        request: MCPOrchestrationRequest,
        *,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> MCPOrchestrationResult: ...

    def prepare_run(
        self,
        request: MCPOrchestrationRequest,
    ) -> dict[str, Any] | None:
        """Prépare (crée ou retrouve) le run durable AVANT l'exécution.

        L1 (SCRUM-152) : le transport SSE doit exposer ``run_id`` DÈS
        l'événement ``orchestrate.started`` (et pouvoir annuler le run si le
        client clique Stop). La préparation est donc séparée de ``run`` :
        elle crée le run (statut ``pending``) ou retourne le run existant, sans
        transition ni incrément de reprise. ``None`` si le stockage durable est
        indisponible (le run reste alors non durable).
        """
        ...

    def cancel(
        self,
        run_id: str,
        *,
        reason: str | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> MCPDurableRunState: ...

    def get_run(self, run_id: str) -> dict[str, Any] | None: ...

    def get_events(self, run_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]: ...

    def list_runs(self, *, state: str | None = None, limit: int = 50) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class MCPRemoteCall:
    """Validated outbound MCP request."""

    server: str
    method: str
    params: dict[str, Any]
    timeout: float = 10.0


@dataclass(frozen=True)
class MCPHostTool:
    """Stable local tool projection for an MCP host backend."""

    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool = True


@runtime_checkable
class MCPHostPort(Protocol):
    """Domain contract for an outbound MCP host."""

    def list_tools(self, server: str | None = None) -> list[MCPHostTool]: ...

    def call(self, request: MCPRemoteCall) -> dict[str, Any]: ...

    def health(self) -> dict[str, Any]: ...

    def stop(self) -> None: ...


@runtime_checkable
class MCPToolRegistryPort(Protocol):
    """Contrat de projection des tools MCP (MCP ``tools/list`` + ``tools/call``).

    Remplace le ``ToolProvider`` infrastructurel (docs/mcp/IMPLEMENTATION_PLAN.md,
    tâche 3) : source de vérité des tools exposés, avec scope de sécurité
    (``MCPScopeRole`` intégré à chaque ``MCPTool``).

    Alignement : les méthodes ``list_tools`` / ``call_tool`` de
    ``ToolProvider`` (``mcp_server.py``) — un simple adaptateur projette le
    registre legacy ``ToolRegistry`` (ia/tools/tool_registry.py) sur ce port
    à la S2 (tâche 6 : 12 tools read-only).
    """

    def list_tools(self) -> list[MCPTool]:
        """Tous les tools connus (y compris masqués par le scope).

        Le filtrage par scope (``required_scope`` vs rôle client) relève de
        l'infrastructure (``MCPServer._visible_tools``) : le port rend la
        vérité, le serveur projette la vue sécurisée.
        """
        ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Exécute un tool par son nom.

        Lève ``ToolError`` (erreur métier → ``isError: true``) pour un tool
        connu qui échoue ; un nom inconnu est traité comme ``Invalid Params``
        par le serveur. L'isollement d'erreurs (catch-all) est de la
        responsabilité de l'implémentation.
        """
        ...


@runtime_checkable
class MCPResourceRegistryPort(Protocol):
    """Contrat des ressources MCP (MCP ``resources/list`` + ``resources/read``).

    « URI templates ↔ tools » : chaque template ``thinktuning://jobs/{job_id}``
    est résolu à la lecture par ``read_resource`` (via un tool backend). La
    liste des resources sert à ``resources/list`` dans l'initialisation du
    handshake MCP ; les gabarits paramétrés sont distingués côté provider
    (``list_resource_templates``, préparation ``resources/templates/list``).

    Implémentation livrée en S3 (tâche 8 : 5 resources ``thinktuning://`` via
    ``LegacyResourceProvider``) ; extension en S5 (tâche 11 : 10 resources).

    Règles :
        - la liste est de la MÉTADONNÉE pure (aucune I/O) : le catalogue se
          construit sans toucher aux tools backend ;
        - ``read_resource`` lève ``NotFoundError`` (404) si l'URI est inconnue
          ou si la cible l'est (job/dataset absent, chemin hors sandbox) — le
          serveur traduit en erreur JSON-RPC ; l'anti-traversée et la lecture
          seule (``safe_resolve``, SQLite ``query_only``) restent portées par
          l'implémentation (délégation aux tools legacy).
    """

    def list_resources(self) -> list[MCPResource]:
        """Resources exposées (statiques + gabarits des paramétrées)."""
        ...

    def read_resource(self, uri: str) -> str:
        """Résout une URI concrète en contenu texte (JSON sérialisé).

        Lève ``NotFoundError`` (404) si l'URI est inconnue ou non autorisée
        pour le scope du client. L'anti-SSRF et la validation de chemin
        restent de la responsabilité de l'implémentation.
        """
        ...


@runtime_checkable
class MCPPromptRegistryPort(Protocol):
    """Contrat des prompts MCP (MCP ``prompts/list`` + ``prompts/get``).

    Un prompt est un template nommé : ``list_prompts`` rend le catalogue ;
    ``get_prompt`` résout les arguments en messages (role + content).

    La vraie implémentation arrive en S3 (tâche 9 : 2 prompts) ; le port
    est défini maintenant pour valider le contrat avant l'implémentation.
    """

    def list_prompts(self) -> list[MCPPromptTemplate]:
        """Catalogue des prompts statiques exposés."""
        ...

    def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> list[MCPPromptMessage]:
        """Résout un prompt en messages (role + content).

        Lève ``NotFoundError`` (404) si le nom est inconnu ; lève
        ``ValidationError`` (422) si un argument requis
        (``MCPPromptArgument.required``) est manquant.
        """
        ...


@runtime_checkable
class SamplingPort(Protocol):
    """Reverse LLM inference — MCP ``sampling/create`` (tache 15, S6 v2.0.0).

    Le serveur MCP agit comme CLIENT de son propre LLM sur demande d'un
    client MCP : celui-ci fournit les messages et les preferences, le
    serveur genere du texte via ``SamplingPort``.

    Deux niveaux (compatibilite S1 -> S6) :
      - ``create_message(request)`` — contrat CANONIQUE (tache 15) :
        entree validee ``SamplingRequest`` -> sortie typee
        ``SamplingResponse`` ;
      - ``create_text(...)`` — commodite S1 (``str`` direct) ; les
        implementations DOIVENT le fournir par delegation a
        ``create_message`` (defaut via ``_sampling_create_text``).

    Le scope ``sampling_enabled`` (S4, MCP_SECURITY.md) controle l'acces ;
    ``LLMClientError`` (domaine) en cas d'echec provider -> le serveur MCP
    traduit en ``error`` JSON-RPC (code -32603).
    """

    def create_message(self, request: SamplingRequest) -> SamplingResponse:
        """Genere une completion typee depuis une requete validee."""
        ...

    def create_text(
        self,
        messages: list[Message],
        *,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Genere du texte brut (commodite deleguant a ``create_message``)."""
        ...


def _sampling_create_text(
    port: SamplingPort,
    messages: list[Message],
    *,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    """Implémentation par défaut de ``create_text`` via ``create_message``.

    Construit la ``SamplingRequest`` (validation Pydantic fail-fast),
    délègue au contrat canonique, et ne rend que ``response.text``.
    Factorisée ici pour que chaque adaptateur l'utilise sans duplication.
    """
    request = SamplingRequest(
        messages=[dict(m) for m in messages],
        max_tokens=max_tokens,
        system_prompt=system_prompt,
        temperature=temperature,
    )
    return port.create_message(request).text


# ============================================================
# MCP SECURITY SCOPE  (S4 — Tâche 10)
# ============================================================
# Modèle de domaine PUR (Pydantic v2, frozen, extra="forbid") :
# définit le périmètre d'autorisation d'un client MCP (client_id,
# tenant, rôle, listes de visibilité, quotas, révocation).
#
# Ce modèle est la SOURCE DE VÉRITÉ du scope : il est produit par
# ``MCPClientStore.register`` et consommé par l'infrastructure
# (``scope_enforcer.py``) pour filtrer tools / resources / prompts
# et appliquer les quotas. Le domaine ne connaît pas le transport
# ni le framework — juste la définition du scope.


class MCPSecurityScope(BaseModel):
    """Périmètre de sécurité d'un client MCP (docs/mcp/MCP_SECURITY.md).

    Chaque client MCP est **toujours** associé à un scope. Le scope limite
    ce que le client peut voir (tools, resources, prompts) et faire
    (sampling, quotas destructifs, limite de débit).

    Attributs :
        client_id : identifiant unique du client MCP (ex. ``"claude-desktop-prod"``).
        tenant_id : tenant / environnement (``"default"``, ``"staging"``, ``"production"``).
        role : rôle de sécurité (``MCPScopeRole``) — ordonné du plus restrictif
            au plus permissif : ``read_only`` < ``contributor`` < ``operator`` < ``admin``.
        visible_tools : whitelist des noms de tools visibles (vide = tous les tools
            dont le ``required_scope`` ≤ rôle du client, filtré à l'infrastructure).
        visible_resources : whitelist des URI patterns ou noms de resources visibles.
        visible_prompts : whitelist des noms de prompts visibles.
        sampling_enabled : autorise le MCP ``sampling/create`` (``False`` par défaut).
        rate_limit_per_minute : débit maximal (appels/min) — 60 par défaut, 600 admin,
            1200 CI.
        destructive_quota : nombre maximal d'outils "manual approval" / heure
            (5 par défaut) — les tools marqués ``destructiveHint`` passent par
            cette quota.
        revoked : le client a été révoqué (accès immédiatement refusé, HTTP 401).
        revoked_at : horodatage UTC de la révocation (``None`` si non révoqué).
        revoked_reason : motif de la révocation (ex. ``"compromised_token"``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_id: str = Field(
        ...,
        min_length=1,
        description="Identifiant unique du client MCP (ex. 'claude-desktop-prod').",
    )
    tenant_id: str = Field(
        default="default",
        min_length=1,
        description="Tenant / environnement ('default', 'staging', 'production').",
    )
    role: str = Field(
        ...,
        description="Rôle de sécurité ('read_only', 'contributor', 'operator', 'admin').",
    )
    visible_tools: list[str] = Field(
        default_factory=list,
        description="Whitelist des tools visibles (vide = tous autorisés par rôle).",
    )
    visible_resources: list[str] = Field(
        default_factory=list,
        description="Whitelist des URI patterns / noms de resources visibles.",
    )
    visible_prompts: list[str] = Field(
        default_factory=list,
        description="Whitelist des noms de prompts visibles.",
    )
    sampling_enabled: bool = Field(
        default=False,
        description="Autorise MCP sampling/create (False par défaut, True pour operator+).",
    )
    rate_limit_per_minute: int = Field(
        default=60,
        ge=1,
        le=10000,
        description="Débit maximal (appels/min) : 60 default, 600 admin, 1200 CI.",
    )
    destructive_quota: int = Field(
        default=5,
        ge=0,
        le=1000,
        description="Quota max d'outils 'manual approval' / heure (5 default).",
    )
    revoked: bool = Field(
        default=False,
        description="Le client est révoqué (accès immédiatement refusé, HTTP 401).",
    )
    revoked_at: datetime | None = Field(
        default=None,
        description="Horodatage UTC de la révocation (None si non révoqué).",
    )
    revoked_reason: str = Field(
        default="",
        description="Motif de la révocation (ex. 'compromised_token').",
    )

    # --- Helpers de domaine ---------------------------------------------------

    @property
    def is_active(self) -> bool:
        """Le scope est-il actif (non révoqué) ?"""
        return not self.revoked

    def revoke(self, reason: str, *, at: datetime | None = None) -> MCPSecurityScope:
        """Retourne UNE NOUVELLE instance de scope révoqué (immuable).

        Le scope étant ``frozen``, la révocation produit une copie avec
        ``revoked=True``, ``revoked_at`` et ``revoked_reason`` renseignés.
        Les métadonnées de révocation sont validées (raison non vide).
        """
        if not reason or not reason.strip():
            raise ValueError("Le motif de révocation ne peut pas être vide.")
        now = at or datetime.now(UTC)
        return self.model_copy(
            update={
                "revoked": True,
                "revoked_at": now,
                "revoked_reason": reason.strip(),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        """Représentation sérialisable (ISO 8601 pour ``revoked_at``)."""
        d = self.model_dump()
        if d.get("revoked_at") is not None:
            d["revoked_at"] = d["revoked_at"].isoformat()
        return d
