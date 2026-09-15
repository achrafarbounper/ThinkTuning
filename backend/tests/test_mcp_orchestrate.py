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
from app.domain.ports import (
    MCPOrchestrationResult,
    compute_mcp_failure_phase,
    compute_mcp_status,
)
from app.domain.entities.plan import Intent
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
        ({"workers": [{"status": "failed"}], "worker_errors": [{"worker_id": "w1", "code": "timeout"}]}, "partial_success", "worker"),
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
    assert recorder._normalize_event_data("mcp.orchestrate.start", {"message": "start"})["parent_task_id"] == "parent-1"
    assert recorder._normalize_event_data("mcp.orchestrate.worker", {"message": "worker_detail"}) == {}
    assert recorder._normalize_event_data("mcp.done", {"answer": "ok"})["worker_id"] == "worker-7"


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
