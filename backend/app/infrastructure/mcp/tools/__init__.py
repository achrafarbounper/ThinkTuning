# project/app/infrastructure/mcp/tools/__init__.py
"""Tools MCP ThinkTuning (tâche 16 — S6, v2.0.0).

- ``orchestrate_tool`` : tool ``orchestrate`` — orchestration agentique
  complète (``AgentCore.run`` via ``build_agent_core``), tool MCP DISTINCT
  des tools bruts du registre legacy : le client donne une demande libre,
  l'agent planifie/appelle ses outils et répond. SÉCURITÉ : chaque action du
  run passe par ``sandbox_policy.decide_action()`` — toute mutation
  (write/delete/exec) exige une validation humaine (``APPROVE`` →
  ``pending_approval``, action surfacee par ``awaiting_approval``).
"""

from __future__ import annotations

from app.infrastructure.mcp.tools.orchestrate_tool import (
    ORCHESTRATE_TOOL_NAME,
    build_orchestrate_tool,
    orchestrate,
)

__all__ = ["ORCHESTRATE_TOOL_NAME", "build_orchestrate_tool", "orchestrate"]
