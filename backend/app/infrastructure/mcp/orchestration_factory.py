"""Production composition for the MCP orchestration application adapter."""

from __future__ import annotations

from app.application.mcp_orchestration import MultiAgentMCPAdapter
from app.domain.ports import MCPOrchestrationPort
from app.infrastructure.legacy_multi_agent_adapter import build_multi_agent_orchestrator
from app.infrastructure.persistence.mcp_mongo_run_store import MongoMCPDurableRunStore


def build_mcp_orchestration_adapter() -> MCPOrchestrationPort:
    """Build the application adapter with production infrastructure ports."""
    return MultiAgentMCPAdapter(
        build_multi_agent_orchestrator(),
        durable_store=MongoMCPDurableRunStore(),
    )
