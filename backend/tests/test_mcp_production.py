from __future__ import annotations

import json

from app.domain.entities.mcp import MCPScopeRole, MCPVersion, SamplingResponse
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server


class _ProductionSampling:
    def create_message(self, request):
        return SamplingResponse(text="ok", model="test")

    def create_text(self, messages, **kwargs):
        return "ok"


class _ProductionDurablePort:
    def get_run(self, run_id):
        return {"run_id": run_id, "state": "completed", "events": []}

    def get_events(self, run_id, *, after_sequence=0):
        return []

    def list_runs(self, *, state=None, limit=50):
        return []

    def cancel(self, run_id, *, reason=None, on_event=None):
        raise AssertionError("cancel is not called by the production surface test")


def _rpc(server, request_id, method, params=None):
    return json.loads(
        server.handle_text(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params or {},
                }
            )
        )
    )


def test_production_mcp_surface_is_complete_and_scoped() -> None:
    server = build_mcp_server(
        scope=MCPScopeRole.ADMIN,
        version=MCPVersion(major=2, minor=2, patch=0),
        sampling_port=_ProductionSampling(),
        orchestration_port=_ProductionDurablePort(),
        durable_run_tools=True,
    )

    initialized = _rpc(server, 1, "initialize")
    assert "sampling" in initialized["result"]["capabilities"]

    tools = _rpc(server, 2, "tools/list")["result"]["tools"]
    names = {tool["name"] for tool in tools}
    assert len(names) == 47
    assert {
        "orchestrate",
        "orchestrate_get_run",
        "orchestrate_list_runs",
        "orchestrate_cancel",
        "orchestrate_events",
    } <= names

    resources = _rpc(server, 3, "resources/list")["result"]["resources"]
    prompts = _rpc(server, 4, "prompts/list")["result"]["prompts"]
    assert len(resources) == 10
    assert len(prompts) == 5

    events_tool = next(tool for tool in tools if tool["name"] == "orchestrate_events")
    assert "after_sequence" in events_tool["inputSchema"]["properties"]

    run = _rpc(
        server,
        5,
        "tools/call",
        {"name": "orchestrate_get_run", "arguments": {"run_id": "run-1"}},
    )
    assert run["result"]["isError"] is True
    assert "Manual approval required" in run["result"]["content"][0]["text"]


def test_read_only_cannot_see_durable_cancellation() -> None:
    server = build_mcp_server(
        scope=MCPScopeRole.READ_ONLY,
        version=MCPVersion(major=2, minor=0, patch=0),
        orchestration_port=_ProductionDurablePort(),
        durable_run_tools=True,
    )
    names = {
        tool["name"]
        for tool in _rpc(server, 1, "tools/list")["result"]["tools"]
    }
    assert "orchestrate_get_run" in names
    assert "orchestrate_events" in names
    assert "orchestrate_cancel" not in names
