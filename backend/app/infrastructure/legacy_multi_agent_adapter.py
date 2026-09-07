"""Adaptateur : orchestrateur multi-agents legacy (core/agent_cache.py) -> port.

Encapsule les fonctions ``ask_multi_agent`` / ``ask_multi_agent_streaming``
derrière ``MultiAgentOrchestratorPort`` sans aucune logique nouvelle.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.domain.ports import MultiAgentOrchestratorPort

try:
    from core.agent_cache import (
        ask_multi_agent as _legacy_run,
    )
    from core.agent_cache import (
        ask_multi_agent_streaming as _legacy_streaming,
    )
except ImportError as _exc:
    raise ImportError(
        "core.agent_cache introuvable : adaptateur multi-agents inutilisable."
    ) from _exc


class LegacyMultiAgentAdapter:
    """Implémentation de ``MultiAgentOrchestratorPort`` au-dessus du legacy."""

    def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        parallel: bool = False,
        resume_request_id: str | None = None,
        enable_thinking: bool = False,
    ) -> dict[str, Any]:
        return _legacy_run(
            prompt,
            model=model,
            parallel=parallel,
            resume_request_id=resume_request_id,
            enable_thinking=enable_thinking,
        )

    def run_streaming(
        self,
        prompt: str,
        *,
        model: str | None = None,
        parallel: bool = False,
        resume_request_id: str | None = None,
        enable_thinking: bool = False,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        return _legacy_streaming(
            prompt,
            model=model,
            parallel=parallel,
            resume_request_id=resume_request_id,
            enable_thinking=enable_thinking,
            on_event=on_event,
        )


def build_multi_agent_orchestrator() -> MultiAgentOrchestratorPort:
    """Instance par défaut (legacy sous-jacent)."""
    return LegacyMultiAgentAdapter()
