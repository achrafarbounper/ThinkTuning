"""Use case de l'orchestration multi-agents (façade mince).

Enveloppe ``MultiAgentOrchestratorPort`` (qui délègue à
``core.agent_cache.ask_multi_agent[_streaming]``) pour offrir une interface
propre dans ``app/application/``. Le cœur complexe (plan → dispatch → synthèse,
budget, FSM, reprise) reste dans ``ia/agent/orchestrator.py`` — ce use case
est une façade strangler, pas une réécriture.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.domain.ports import MultiAgentOrchestratorPort


def run_multi_agent(
    port: MultiAgentOrchestratorPort,
    prompt: str,
    *,
    model: str | None = None,
    parallel: bool = False,
    resume_request_id: str | None = None,
    enable_thinking: bool = False,
) -> dict[str, Any]:
    """Exécute le cycle complet multi-agents (bloquant)."""
    return port.run(
        prompt,
        model=model,
        parallel=parallel,
        resume_request_id=resume_request_id,
        enable_thinking=enable_thinking,
    )


def run_multi_agent_streaming(
    port: MultiAgentOrchestratorPort,
    prompt: str,
    *,
    model: str | None = None,
    parallel: bool = False,
    resume_request_id: str | None = None,
    enable_thinking: bool = False,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Exécute le cycle complet multi-agents (streaming SSE)."""
    return port.run_streaming(
        prompt,
        model=model,
        parallel=parallel,
        resume_request_id=resume_request_id,
        enable_thinking=enable_thinking,
        on_event=on_event,
    )
