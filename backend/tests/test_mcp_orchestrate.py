# project/tests/test_mcp_orchestrate.py
"""Tests du tool MCP `orchestrate` (S6 — Tâche 16, docs/mcp/IMPLEMENTATION_PLAN.md).

Contrat vérifié :
    1. ``orchestrate(prompt, session_id, scope)`` wrappe ``AgentCore.run()`` :
       retourne ``AgentRunResult`` (``answer`` + traces ``actions`` + budget) ;
    2. transfert fidèle de ``session_id`` / ``scope`` vers ``Intent`` ;
    3. SÉCURITÉ : chaque action du run passe par ``decide_action()`` —
       une mutation (catégorie UNKNOWN incluse) ressort ``APPROVE`` → le run
       se termine en ``pending_approval`` avec l'action en attente
       (``awaiting_approval: true``), jamais exécutée sans validation humaine ;
    4. ``orchestrate`` est un tool MCP DISTINCT des tools bruts : MCPTool
       autonome (annotations mutation, rôle CONTRIBUTOR+), exposé par
       ``build_mcp_server`` à partir de la v2.0.0, absent avant ;
    5. audit : ``tools/call`` sur ``orchestrate`` → ``ACT_MCP_ORCHESTRATE``.

Aucun import lourd (ni torch, ni transformers) — fakes LLM/registre identiques
à ``tests/test_agent_core.py``, la fabrique du noyau est injectée.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agent.core import AgentCore, AgentRunResult, RunStatus
from app.domain.entities.mcp import MCPScopeRole, MCPTool, MCPVersion
from app.domain.entities.plan import Intent
from app.domain.ports import (
    MCPOrchestrationResult,
    compute_mcp_failure_phase,
    compute_mcp_status,
)
from app.infrastructure.mcp.manifest_generator import MUTATING_ANNOTATIONS
from app.infrastructure.mcp.mcp_server import ToolError
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.protocol import ErrorCode
from app.infrastructure.mcp.tools.orchestrate_tool import (
    ORCHESTRATE_TOOL_NAME,
    build_orchestrate_tool,
    orchestrate,
)
from app.infrastructure.persistence.audit_store import ACT_MCP_ORCHESTRATE


class ScriptedLLM:
    """LLM fake : renvoie les réponses dans l'ordre, enregistre les messages."""

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.messages: list[list[dict]] = []

    def call(self, messages):
        self.messages.append(messages)
        return self.replies.pop(0)

    def call_stream(self, messages, on_thinking=None, on_content=None):
        return self.call(messages)


class FakeRegistry:
    """Registre fake minimal : ``now`` (lecture) et ``echo`` (catégorie UNKNOWN)."""

    def tool_names(self):
        return ["now", "echo"]

    def get(self, tool):
        table = {
            "now": lambda: "2026-09-02T12:00:00Z",
            "echo": lambda text="": text,
        }
        return table.get(tool)

    def meta(self, tool):
        return {"description": f"outil {tool}", "required_args": []}


def _core_factory(llm: ScriptedLLM):
    """Fabrique du noyau debranchée sur des fakes (LLM + registre)."""

    def _build() -> AgentCore:
        return AgentCore(llm, FakeRegistry())

    return _build


# ============================ 1. orchestrate() ==================================


def test_orchestrate_returns_answer_and_traces() -> None:
    """``orchestrate`` wrappe ``AgentCore.run`` : réponse + traces d'actions."""
    llm = ScriptedLLM([
        '{"plan": [{"tool": "now", "args": {}}]}',
        "Le dataset contient 1200 lignes.",
    ])
    result = orchestrate("analyse ce dataset", "s1", "lead", core_factory=_core_factory(llm))
    assert isinstance(result, AgentRunResult)
    assert result.status is RunStatus.COMPLETED
    assert result.answer == "Le dataset contient 1200 lignes."
    assert len(result.actions) == 1
    assert result.actions[0].tool == "now"
    assert result.actions[0].status == "done"
    assert result.actions[0].decision == "auto_approve"
    assert result.rounds_used == 2
    assert result.tool_calls_used == 1


class SpyCore:
    """Fake d'``AgentCore`` : enregistre l'``Intent`` reçu et répond de façon fixe."""

    def __init__(self) -> None:
        self.intents: list[Intent] = []

    def run(self, intent: Intent) -> AgentRunResult:
        self.intents.append(intent)
        return AgentRunResult(answer="ok", status=RunStatus.COMPLETED)


def test_orchestrate_forwards_session_id_and_scope_to_intent() -> None:
    """``session_id`` et ``scope`` sont projetés sur ``Intent`` (rôle agent)."""
    spy = SpyCore()
    orchestrate("analyse ce dataset", "session-42", "lead", core_factory=lambda: spy)  # type: ignore[return-value]
    (intent,) = spy.intents
    assert intent.prompt == "analyse ce dataset"
    assert intent.session_id == "session-42"
    assert intent.role == "lead"


def test_orchestrate_defaults_session_and_scope() -> None:
    """Sans ``session_id``/``scope`` → valeurs par défaut (jamais None)."""
    spy = SpyCore()
    orchestrate("simple demande", core_factory=lambda: spy)  # type: ignore[return-value]
    (intent,) = spy.intents
    assert intent.session_id == "default"
    assert intent.role == "default"


def test_orchestrate_mutation_requires_human_approval() -> None:
    """SÉCURITÉ : action non auto-approuvable (echo = UNKNOWN → APPROVE)
    → le run se termine en ``pending_approval``, l'action n'est PAS exécutée
    et reste en attente de validation humaine (``awaiting_approval``)."""
    llm = ScriptedLLM(['{"plan": [{"tool": "echo", "args": {"text": "x"}}]}'])
    result = orchestrate("fais une action", "s1", core_factory=_core_factory(llm))
    assert result.status is RunStatus.PENDING_APPROVAL
    assert result.awaiting_action is not None
    assert result.awaiting_action.tool == "echo"
    assert result.actions[0].status == "awaiting_approval"
    assert result.actions[0].decision == "approve"
    assert result.answer == "En attente de validation humaine."


# ==================== 4. Tool MCP distinct des tools bruts =====================


def test_build_orchestrate_tool_declares_distinct_mcp_tool() -> None:
    """``orchestrate`` est un ``MCPTool`` autonome : mutation, CONTRIBUTOR+,
    schéma exigeant ``prompt`` — pas un tool brut du registre legacy."""
    tool = build_orchestrate_tool(core_factory=_core_factory(ScriptedLLM([])))
    assert isinstance(tool, MCPTool)
    assert tool.name == ORCHESTRATE_TOOL_NAME
    assert tool.required_scope is MCPScopeRole.CONTRIBUTOR
    assert tool.annotations == dict(MUTATING_ANNOTATIONS)
    assert tool.input_schema["required"] == ["prompt"]
    properties = tool.input_schema["properties"]
    assert {
        "prompt", "session_id", "scope", "enable_thinking",
        "mode", "model", "parallel", "event_granularity", "resume_request_id",
    } <= set(properties)


def test_tool_handler_normalizes_multi_agent_result(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeOrchestrator:
        def run(self, request: Any, *, on_event: Any = None) -> MCPOrchestrationResult:
            assert request.prompt == "decompose this"
            assert request.parallel is True
            return MCPOrchestrationResult(
                answer="synthesized",
                status="partial_success",
                workers=[{"id": "w1", "status": "failed"}],
                worker_errors=[{"id": "w1", "status": "failed"}],
                orchestration={"mode": "multi_agent"},
            )

    monkeypatch.setenv("MCP_MULTI_AGENT_ENABLED", "1")
    tool = build_orchestrate_tool(orchestrator=FakeOrchestrator())
    payload = json.loads(tool.handler({
        "prompt": "decompose this",
        "mode": "multi_agent",
        "parallel": True,
    }))
    assert payload["answer"] == "synthesized"
    assert payload["status"] == "partial_success"
    assert payload["worker_errors"][0]["id"] == "w1"


def test_mcp_orchestration_result_contract_is_additive_and_stable() -> None:
    """The multi-agent contract adds explicit fields without breaking existing ones."""
    result = MCPOrchestrationResult(
        answer="synthesized",
        status="partial_success",
        plan=[{"id": "plan-1", "status": "completed"}],
        subtasks=[{"id": "sub-1", "worker_id": "w1", "status": "failed"}],
        workers=[{"id": "w1", "status": "failed"}],
        synthesis={"status": "completed", "summary": "partial result"},
        worker_errors=[{"worker_id": "w1", "code": "timeout"}],
        usage={"runtime_ms": 1200},
        orchestration={"mode": "multi_agent"},
    )
    dumped = result.model_dump(mode="json")
    assert dumped["answer"] == "synthesized"
    assert dumped["status"] == "partial_success"
    assert dumped["plan"][0]["id"] == "plan-1"
    assert dumped["subtasks"][0]["worker_id"] == "w1"
    assert dumped["workers"][0]["status"] == "failed"
    assert dumped["synthesis"]["summary"] == "partial result"
    assert dumped["worker_errors"][0]["code"] == "timeout"


@pytest.mark.parametrize(
    ("payload", "expected_status", "expected_phase"),
    [
        ({"lead": {"status": "failed"}}, "failed", "lead"),
        (
            {
                "workers": [{"status": "failed"}],
                "worker_errors": [{"worker_id": "w1", "code": "timeout"}],
            },
            "partial_success",
            "worker",
        ),
        ({"synthesis": {"status": "failed"}}, "failed", "synthesis"),
    ],
)
def test_compute_mcp_status_decides_global_status(
    payload: dict[str, Any],
    expected_status: str,
    expected_phase: str,
) -> None:
    """The global status policy distinguishes lead/worker/synthesis failures."""
    assert compute_mcp_status(**payload) == expected_status
    assert compute_mcp_failure_phase(**payload) == expected_phase

    result = MCPOrchestrationResult(
        answer="partial",
        status=expected_status,
        failure_phase=expected_phase,
    )
    assert result.failure_phase == expected_phase
    assert result.status == expected_status


def test_tool_handler_falls_back_when_shared_settings_disable_multi_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.infrastructure.persistence.agent_settings as agent_settings

    class FakeStore:
        def get_all(self) -> dict[str, Any]:
            return {"flag_multi_agent": False}

    monkeypatch.delenv("MCP_MULTI_AGENT_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_MULTI_AGENT", raising=False)
    monkeypatch.setattr(agent_settings, "get_settings_store", lambda: FakeStore())

    tool = build_orchestrate_tool(core_factory=_core_factory(ScriptedLLM([])))
    payload = json.loads(tool.handler({
        "prompt": "decompose this",
        "mode": "multi_agent",
    }))
    assert payload["orchestration"]["event"] == "orchestration_fallback"
    assert payload["orchestration"]["fallback"] == "orchestration_fallback"
    assert payload["orchestration"]["reason"] == "multi_agent_disabled"


def test_execution_context_rejects_worker_scope_expansion() -> None:
    """{ExecutionContext} is the shared source of truth for MCP/HTTP worker scope."""
    from app.domain.ports import ExecutionContext

    context = ExecutionContext(
        user_id="u1",
        tenant_id="tenant-1",
        allowed_tools=("read", "write"),
        allowed_resources=("session://default",),
        allowed_scopes=("lead",),
    )
    with pytest.raises(ValueError, match="widen"):
        context.for_worker(
            "researcher",
            allowed_tools=("read", "write", "exec"),
            allowed_resources=("session://default",),
            allowed_scopes=("lead",),
        )


# ============= 6. Décision mutualisée + HITL MCP (P0 — SCRUM-151) ==============


class _FakeApprovalStore:
    """Store d'approbations en mémoire (même surface que ``ApprovalStore``)."""

    def __init__(self, records: dict[str, dict[str, Any]] | None = None) -> None:
        self.records: dict[str, dict[str, Any]] = dict(records or {})

    def create(
        self,
        tool: str,
        args: dict[str, Any],
        category: str,
        decision: str,
        reason: str,
        prompt: str = "",
        args_hash: str = "",
        status: str = "pending",
    ) -> str:
        request_id = f"req-{len(self.records) + 1}"
        self.records[request_id] = {
            "request_id": request_id,
            "tool": tool,
            "args": args,
            "category": category,
            "status": status,
            "args_hash": args_hash,
        }
        return request_id

    def get(self, request_id: str) -> dict[str, Any] | None:
        return self.records.get(request_id)


def test_resolve_orchestration_defaults_and_validations() -> None:
    """Mode par défaut mono-agent ; ``mode``/granularité invalides → ValueError."""
    from app.infrastructure.mcp.tools.orchestrate_tool import resolve_orchestration

    resolution = resolve_orchestration({"prompt": "x"})
    assert resolution.mode == "mono_agent"
    assert resolution.requested_mode == "mono_agent"
    assert resolution.fallback is None
    assert resolution.event_granularity == "summary"
    assert not resolution.is_resuming
    with pytest.raises(ValueError, match="mode"):
        resolve_orchestration({"mode": "bogus"})
    with pytest.raises(ValueError, match="event_granularity"):
        resolve_orchestration({"event_granularity": "all"})


def test_resolve_orchestration_decoupled_identifiers() -> None:
    """``run_id`` (run durable) et ``resume_request_id`` (approbation) sont
    transportés séparément — la reprise ciblée ajoute ``task_id``."""
    from app.infrastructure.mcp.tools.orchestrate_tool import resolve_orchestration

    resolution = resolve_orchestration({
        "prompt": "x",
        "run_id": "run-77",
        "resume_request_id": "req-9",
        "task_id": "t1",
    })
    assert resolution.is_resuming
    assert resolution.run_id == "run-77"
    assert resolution.resume_request_id == "req-9"
    assert resolution.task_id == "t1"
    arguments = resolution.as_arguments()
    assert arguments["run_id"] == "run-77"
    assert arguments["resume_request_id"] == "req-9"


def test_resolve_orchestration_decisions_identical_for_both_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PARITÉ stream/non-stream : mêmes arguments → résolution identique.

    Le transport SSE et le handler du tool consomment la même fonction : la
    décision (mode, repli, granularité, identifiants) ne peut plus diverger.
    """
    from app.infrastructure.mcp.tools.orchestrate_tool import resolve_orchestration

    monkeypatch.setenv("MCP_MULTI_AGENT_ENABLED", "1")
    arguments = {
        "prompt": "x",
        "mode": "multi_agent",
        "parallel": True,
        "event_granularity": "verbose",
        "run_id": "run-77",
        "resume_request_id": "req-9",
        "task_id": "t1",
    }
    stream_decision = resolve_orchestration(arguments)
    non_stream_decision = resolve_orchestration(dict(arguments))
    assert stream_decision.as_arguments() == non_stream_decision.as_arguments()
    assert stream_decision.mode == non_stream_decision.mode == "multi_agent"
    assert stream_decision.fallback is None


def test_resolve_orchestration_falls_back_when_flag_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garde ``MCP_MULTI_AGENT_ENABLED`` : repli EXPLICITE ``multi_agent_disabled``."""
    import app.infrastructure.persistence.agent_settings as agent_settings
    from app.infrastructure.mcp.tools.orchestrate_tool import resolve_orchestration

    class FakeStore:
        def get_all(self) -> dict[str, Any]:
            return {"flag_multi_agent": False}

    monkeypatch.delenv("MCP_MULTI_AGENT_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_MULTI_AGENT", raising=False)
    monkeypatch.setattr(agent_settings, "get_settings_store", lambda: FakeStore())

    resolution = resolve_orchestration({"prompt": "x", "mode": "multi_agent"})
    assert resolution.mode == "mono_agent"
    assert resolution.requested_mode == "multi_agent"
    assert resolution.fallback is not None
    assert resolution.fallback["event"] == "orchestration_fallback"
    assert resolution.fallback["reason"] == "multi_agent_disabled"


def test_worker_scope_policy_rejects_forbidden_tools_and_budget() -> None:
    """``WorkerScopePolicy`` APPLIQUE réellement outils interdits + plafond."""
    from app.domain.ports import ExecutionContext, WorkerScopePolicy

    policy = WorkerScopePolicy(
        parent_scope=("lead",),
        forbidden_tools=("system_shell",),
        max_tools_per_worker=2,
    )

    def _context(tools: tuple[str, ...]) -> ExecutionContext:
        return ExecutionContext(
            user_id="u1",
            tenant_id="tenant-1",
            allowed_tools=tools,
            allowed_resources=("session://default",),
            allowed_scopes=("lead",),
        )

    with pytest.raises(ValueError, match="forbidden"):
        policy.validate(_context(("read", "system_shell")), worker_id="w1")
    with pytest.raises(ValueError, match="max_tools_per_worker"):
        policy.validate(_context(("read", "write", "list")), worker_id="w1")
    # Cas nominal : tools autorisés dans le budget → aucune levée.
    policy.validate(_context(("read",)), worker_id="w1")


def test_tool_handler_surfaces_distinct_request_and_run_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HITL MCP : l'approbation expose ``request_id`` ; ``run_id`` reste celui
    passé par le client — deux identifiants distincts dans le payload."""
    import app.infrastructure.persistence.agent_settings as agent_settings
    from app.infrastructure.mcp.tools.orchestrate_tool import build_orchestrate_tool

    class EnabledStore:
        def get_all(self) -> dict[str, Any]:
            return {"flag_multi_agent": True}

    monkeypatch.setattr(agent_settings, "get_settings_store", lambda: EnabledStore())

    llm = ScriptedLLM(['{"plan": [{"tool": "echo", "args": {"text": "x"}}]}'])
    store = _FakeApprovalStore()
    tool = build_orchestrate_tool(core_factory=_core_factory(llm), approval_store=store)
    payload = json.loads(tool.handler({"prompt": "fais une action", "run_id": "run-77"}))
    assert payload["awaiting_approval"] is True
    assert payload["request_id"]
    assert payload["request_id"] != "run-77"
    assert payload["run_id"] == "run-77"
    assert payload["approval"]["tool"] == "echo"


def test_run_mono_agent_resumes_approved_action_by_fingerprint() -> None:
    """Reprise ciblée : la gateway ne débloque que l'action APPROUVÉE (empreinte)."""
    from app.domain.entities.plan import Action
    from app.infrastructure.mcp.tools.orchestrate_tool import run_mono_agent

    action = Action(tool="write_file", args={"path": "x"}, category="write")
    store = _FakeApprovalStore({
        "req-1": {
            "request_id": "req-1",
            "status": "approved",
            "args_hash": action.fingerprint(),
        }
    })
    captured: dict[str, Any] = {}

    class GatewaySpyCore:
        def __init__(self, approval_gateway: Any = None) -> None:
            self.gateway = approval_gateway

        def run(self, intent: Intent) -> AgentRunResult:
            captured["granted"] = bool(self.gateway and self.gateway(action))
            return AgentRunResult(answer="ok", status=RunStatus.COMPLETED)

    outcome = run_mono_agent(
        "fais l'action",
        core_factory=lambda **kwargs: GatewaySpyCore(**kwargs),
        resume_request_id="req-1",
        approval_store=store,
    )
    assert captured["granted"] is True
    assert outcome.result.status is RunStatus.COMPLETED
    assert outcome.request_id is None  # run terminé → aucune nouvelle demande


def test_run_mono_agent_stays_fail_closed_without_approved_request() -> None:
    """Sans demande approuvée correspondante : AUCUNE action débloquée."""
    from app.domain.entities.plan import Action
    from app.infrastructure.mcp.tools.orchestrate_tool import run_mono_agent

    action = Action(tool="write_file", args={"path": "x"}, category="write")
    store = _FakeApprovalStore({
        "req-1": {"request_id": "req-1", "status": "pending", "args_hash": "whatever"}
    })
    captured: dict[str, Any] = {}

    class GatewaySpyCore:
        def __init__(self, approval_gateway: Any = None) -> None:
            self.gateway = approval_gateway

        def run(self, intent: Intent) -> AgentRunResult:
            captured["granted"] = bool(self.gateway and self.gateway(action))
            return AgentRunResult(answer="bloqué", status=RunStatus.PENDING_APPROVAL)

    run_mono_agent(
        "fais l'action",
        core_factory=lambda **kwargs: GatewaySpyCore(**kwargs),
        resume_request_id="req-1",
        approval_store=store,
    )
    assert captured["granted"] is False


def test_run_mono_agent_persists_approval_request_for_hitl() -> None:
    """Run en attente → demande d'approbation persistée (empreinte enregistrée)."""
    from app.infrastructure.mcp.tools.orchestrate_tool import run_mono_agent

    llm = ScriptedLLM(['{"plan": [{"tool": "echo", "args": {"text": "x"}}]}'])
    store = _FakeApprovalStore()
    outcome = run_mono_agent(
        "fais une action",
        "s1",
        core_factory=_core_factory(llm),
        approval_store=store,
    )
    assert outcome.result.status is RunStatus.PENDING_APPROVAL
    assert outcome.request_id is not None
    assert outcome.approval is not None
    assert outcome.approval["tool"] == "echo"
    record = store.records[outcome.request_id]
    assert record["args_hash"] == outcome.result.awaiting_action.fingerprint()


def test_orchestrate_schema_declares_resumable_identifiers() -> None:
    """Le schéma expose ``run_id`` et ``task_id`` en plus de ``resume_request_id``."""
    tool = build_orchestrate_tool(core_factory=_core_factory(ScriptedLLM([])))
    properties = tool.input_schema["properties"]
    assert {"run_id", "task_id", "resume_request_id"} <= set(properties)


def test_mcp_multi_agent_result_surfaces_awaiting_approval() -> None:
    """Le contrat additif porte l'approbation HITL (run_id ≠ request_id)."""
    result = MCPOrchestrationResult(
        answer="En attente de validation humaine.",
        status="awaiting_approval",
        run_id="run-77",
        workers=[{"id": "w1", "status": "awaiting_approval"}],
        awaiting_approval=True,
        request_id="req-9",
        approval={"request_id": "req-9", "tool": "write_file", "args": {}},
        task_id="t1",
    )
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "awaiting_approval"
    assert dumped["run_id"] == "run-77"
    assert dumped["request_id"] == "req-9"
    assert dumped["run_id"] != dumped["request_id"]
    assert dumped["approval"]["tool"] == "write_file"
    assert dumped["task_id"] == "t1"


def test_mcp_events_include_hierarchy_metadata() -> None:
    """Every MCP event must carry parent_task_id, worker_id, and a canonical phase."""
    from app.domain.ports import normalize_mcp_event

    event = normalize_mcp_event(
        {"kind": "worker_update", "status": "running"},
        parent_task_id="parent-1",
        default_worker_id="worker-7",
        default_phase="worker",
    )
    assert event["phase"] == "worker"
    assert event["parent_task_id"] == "parent-1"
    assert event["worker_id"] == "worker-7"
    assert event["event"] == "worker_update"
    assert event["event_id"].startswith("mcp-")
    assert event["timestamp"]


def test_mcp_orchestration_phase_is_not_collapsed_to_lead() -> None:
    from app.domain.ports import normalize_mcp_event

    event = normalize_mcp_event(
        {"event": "agent.phase", "phase": "orchestration"},
        parent_task_id="run-1",
    )

    assert event["phase"] == "orchestration"


def test_mcp_request_accepts_minimal_event_granularity() -> None:
    """Event granularity is additive and compatible; minimal is allowed."""
    from app.domain.ports import MCPOrchestrationRequest

    request = MCPOrchestrationRequest.from_values(
        prompt="hello",
        event_granularity="minimal",
    )
    assert request.event_granularity == "minimal"


def test_mcp_flow_recorder_filters_detail_events_under_minimal_granularity() -> None:
    """Minimal mode keeps only the terminal / critical events and preserves hierarchy."""
    from app.infrastructure.mcp.mcp_flow import MCPFlowRecorder

    recorder = MCPFlowRecorder(
        "flow-1",
        event_granularity="minimal",
        parent_task_id="parent-1",
        worker_id="worker-7",
    )
    recorder.record("mcp.orchestrate.start", {"message": "start"})
    recorder.record("mcp.orchestrate.worker", {"message": "worker_detail"})
    recorder.record("mcp.done", {"answer": "ok"})
    start_data = recorder._normalize_event_data(
        "mcp.orchestrate.start", {"message": "start"}
    )
    assert start_data["parent_task_id"] == "parent-1"
    assert (
        recorder._normalize_event_data("mcp.orchestrate.worker", {"message": "worker_detail"})
        == {}
    )
    done_data = recorder._normalize_event_data("mcp.done", {"answer": "ok"})
    assert done_data["worker_id"] == "worker-7"


def test_tool_handler_returns_answer_and_traces_json() -> None:
    """Le handler du tool → texte JSON : answer + traces + awaiting_approval."""
    llm = ScriptedLLM([
        '{"plan": [{"tool": "now", "args": {}}]}',
        "Le dataset contient 1200 lignes.",
    ])
    tool = build_orchestrate_tool(core_factory=_core_factory(llm))
    text = tool.handler({
        "prompt": "analyse ce dataset",
        "session_id": "s1",
        "scope": "lead",
    })
    payload = json.loads(text)
    assert payload["answer"] == "Le dataset contient 1200 lignes."
    assert payload["status"] == "completed"
    assert payload["actions"][0]["tool"] == "now"
    assert payload["actions"][0]["status"] == "done"
    assert payload["awaiting_approval"] is False
    assert payload["rounds_used"] == 2


def test_tool_handler_returns_thinking_when_enabled() -> None:
    """Le mode Réflexion MCP expose la trace dans le JSON du tool."""
    class ThinkingCore:
        def run(self, intent: Intent) -> AgentRunResult:
            return AgentRunResult(
                answer="Réponse finale.",
                thinking="Étape de réflexion.",
                status=RunStatus.COMPLETED,
            )

    tool = build_orchestrate_tool(core_factory=ThinkingCore)
    payload = json.loads(tool.handler({
        "prompt": "analyse ce dataset",
        "enable_thinking": True,
    }))
    assert payload["answer"] == "Réponse finale."
    assert payload["thinking"] == "Étape de réflexion."


def test_tool_handler_surfaces_pending_approval() -> None:
    """Mutation APPROVE → le handler du tool surface ``awaiting_approval``."""
    llm = ScriptedLLM(['{"plan": [{"tool": "echo", "args": {"text": "x"}}]}'])
    tool = build_orchestrate_tool(core_factory=_core_factory(llm))
    payload = json.loads(tool.handler({"prompt": "écris un fichier"}))
    assert payload["status"] == "pending_approval"
    assert payload["awaiting_approval"] is True
    assert payload["awaiting_action"]["tool"] == "echo"


def test_tool_handler_missing_prompt_raises_tool_error() -> None:
    """Argument requis manquant → ``ToolError`` (réponse MCP ``isError``)."""
    tool = build_orchestrate_tool(core_factory=_core_factory(ScriptedLLM([])))
    with pytest.raises(ToolError, match="prompt"):
        tool.handler({})
    with pytest.raises(ToolError, match="prompt"):
        tool.handler({"prompt": "   "})


def test_orchestrate_blank_prompt_raises() -> None:
    """``prompt`` vide → ``ValueError`` (argument requis du tool)."""
    with pytest.raises(ValueError, match="prompt"):
        orchestrate("   ", core_factory=lambda: SpyCore())  # type: ignore[return-value]


# ==================== 5. Intégration MCPServer (v2.0.0) =========================


def _rpc(method: str, params: dict) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})


def _v2_server(tool: MCPTool, scope: MCPScopeRole = MCPScopeRole.OPERATOR) -> Any:
    """Serveur MCP sur la surface v2.0.0 avec le tool ``orchestrate`` injecté."""
    return build_mcp_server(
        scope=scope,
        version=MCPVersion(major=2, minor=0, patch=0),
        orchestrate_tool=tool,
    )


def test_server_exposes_orchestrate_tool_from_v2() -> None:
    """v2.0.0 : ``orchestrate`` appaît dans ``tools/list`` (tool DISTINCT)."""
    tool = build_orchestrate_tool(core_factory=_core_factory(ScriptedLLM([])))
    server = _v2_server(tool)
    response = json.loads(server.handle_text(_rpc("tools/list", {})))
    names = [t["name"] for t in response["result"]["tools"]]
    assert ORCHESTRATE_TOOL_NAME in names
    listed = next(t for t in response["result"]["tools"] if t["name"] == ORCHESTRATE_TOOL_NAME)
    assert listed["annotations"] == dict(MUTATING_ANNOTATIONS)


def test_server_exposes_durable_run_tools_with_injected_port() -> None:
    class DurablePort:
        def get_run(self, run_id):
            return {"run_id": run_id, "state": "running", "events": []}

        def list_runs(self, *, state=None, limit=50):
            return [{"run_id": "run-1", "state": state or "running"}][:limit]

        def get_events(self, run_id, *, after_sequence=0):
            return [{"sequence": 1, "event": "started"}][after_sequence:]

        def cancel(self, run_id, *, reason=None, on_event=None):
            return type(
                "State",
                (),
                {
                    "as_snapshot": lambda self: {
                        "run_id": run_id,
                        "state": "cancelled",
                        "reason": reason,
                    }
                },
            )()

    server = build_mcp_server(
        scope=MCPScopeRole.CONTRIBUTOR,
        version=MCPVersion(major=2, minor=0, patch=0),
        orchestration_port=DurablePort(),
        orchestrate_tool=build_orchestrate_tool(
            core_factory=_core_factory(ScriptedLLM([]))
        ),
    )
    listed = json.loads(server.handle_text(_rpc("tools/list", {})))
    names = {tool["name"] for tool in listed["result"]["tools"]}
    assert {
        "orchestrate_get_run",
        "orchestrate_list_runs",
        "orchestrate_cancel",
        "orchestrate_events",
    } <= names

    response = json.loads(
        server.handle_text(
            _rpc(
                "tools/call",
                {
                    "name": "orchestrate_get_run",
                    "arguments": {"run_id": "run-1"},
                },
            )
        )
    )
    assert response["result"]["isError"] is False
    assert json.loads(response["result"]["content"][0]["text"])["run_id"] == "run-1"


def test_server_orchestrate_call_roundtrip() -> None:
    """``tools/call orchestrate`` de bout en bout → réponse JSON (answer + traces)."""
    llm = ScriptedLLM([
        '{"plan": [{"tool": "now", "args": {}}]}',
        "Le dataset contient 1200 lignes.",
    ])
    server = _v2_server(build_orchestrate_tool(core_factory=_core_factory(llm)))
    response = json.loads(
        server.handle_text(
            _rpc("tools/call", {"name": ORCHESTRATE_TOOL_NAME,
                               "arguments": {"prompt": "analyse ce dataset"}})
        )
    )
    assert response["result"]["isError"] is False
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["answer"] == "Le dataset contient 1200 lignes."
    assert payload["status"] == "completed"
    assert payload["actions"][0]["tool"] == "now"


def test_server_default_v2_surface_registers_orchestrate() -> None:
    """v2.0.0 sans injection : ``build_orchestrate_tool()`` est branché par
    défaut (construction du tool sans LLM : le noyau reste paresseux)."""
    server = build_mcp_server(
        scope=MCPScopeRole.OPERATOR,
        version=MCPVersion(major=2, minor=0, patch=0),
    )
    response = json.loads(server.handle_text(_rpc("tools/list", {})))
    names = [t["name"] for t in response["result"]["tools"]]
    assert ORCHESTRATE_TOOL_NAME in names


def test_server_hides_orchestrate_before_v2() -> None:
    """v0.1.0 : ``orchestrate`` n'est ni listé ni appelable (“Unknown tool”)."""
    server = build_mcp_server(version=MCPVersion(major=0, minor=1, patch=0))
    response = json.loads(server.handle_text(_rpc("tools/list", {})))
    names = [t["name"] for t in response["result"]["tools"]]
    assert ORCHESTRATE_TOOL_NAME not in names
    call = json.loads(
        server.handle_text(
            _rpc("tools/call", {"name": ORCHESTRATE_TOOL_NAME,
                               "arguments": {"prompt": "x"}})
        )
    )
    assert call["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert "Unknown tool" in call["error"]["message"]


def test_server_hides_orchestrate_for_read_only_scope() -> None:
    """Filtrage de scope fail-closed : read_only ne voit jamais le tool
    (exigence CONTRIBUTOR — mutation)."""
    tool = build_orchestrate_tool(core_factory=_core_factory(ScriptedLLM([])))
    server = _v2_server(tool, scope=MCPScopeRole.READ_ONLY)
    response = json.loads(server.handle_text(_rpc("tools/list", {})))
    assert ORCHESTRATE_TOOL_NAME not in [t["name"] for t in response["result"]["tools"]]


def test_server_audits_orchestrate_as_mcp_orchestrate() -> None:
    """Audit : ``tools/call`` sur ``orchestrate`` → ``ACT_MCP_ORCHESTRATE``
    (l'audit est tranché par le nom du tool dans ``MCPServer._audit_method``)."""
    events: list[tuple[str, dict]] = []

    def _audit(action: str, **kw: Any) -> None:
        events.append((action, kw))

    llm = ScriptedLLM(['{"plan": [{"tool": "now", "args": {}}]}', "Terminé."])
    server = build_mcp_server(
        scope=MCPScopeRole.OPERATOR,
        version=MCPVersion(major=2, minor=0, patch=0),
        orchestrate_tool=build_orchestrate_tool(core_factory=_core_factory(llm)),
        audit=_audit,
    )
    server.handle_text(
        _rpc("tools/call", {"name": ORCHESTRATE_TOOL_NAME,
                           "arguments": {"prompt": "analyse ce dataset"}}),
        client_id="client-orchestrate",
    )
    assert events, "l'appel orchestrate doit être audité"
    action, detail = events[-1]
    assert action == ACT_MCP_ORCHESTRATE
    assert detail["subject"] == "client-orchestrate"
    assert detail["detail"]["tool"] == ORCHESTRATE_TOOL_NAME
    assert detail["detail"]["is_error"] is False
