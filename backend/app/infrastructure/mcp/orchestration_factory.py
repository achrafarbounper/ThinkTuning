"""Production composition for the MCP orchestration application adapter."""

from __future__ import annotations

import os

from app.application.mcp_orchestration import MultiAgentMCPAdapter
from app.domain.ports import MCPOrchestrationPort
from app.infrastructure.legacy_multi_agent_adapter import build_multi_agent_orchestrator
from app.infrastructure.persistence.mcp_mongo_run_store import MongoMCPDurableRunStore

#: Quota de DURÉE d'un run durable (MCP 2.3.0 — isolation multi-tenant) :
#: un run non terminal au-delà de ce délai est expiré (réessayable via
#: ``runs/retry``). Surchargeable par l'exploitation.
ENV_RUN_MAX_SECONDS = "MCP_RUN_MAX_SECONDS"
DEFAULT_RUN_MAX_SECONDS = 900


def _run_max_seconds() -> int:
    """Quota de durée configuré (secondes) — ``0`` désactive la péremption."""
    raw = os.getenv(ENV_RUN_MAX_SECONDS)
    if raw is None or not str(raw).strip():
        return DEFAULT_RUN_MAX_SECONDS
    try:
        return max(0, int(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_RUN_MAX_SECONDS


def build_mcp_orchestration_adapter() -> MCPOrchestrationPort:
    """Build the application adapter with production infrastructure ports."""
    return MultiAgentMCPAdapter(
        build_multi_agent_orchestrator(),
        durable_store=MongoMCPDurableRunStore(),
        max_run_seconds=_run_max_seconds(),
    )
