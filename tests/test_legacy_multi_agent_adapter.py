"""Tests de l'adaptateur legacy multi-agents."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.domain.ports import MultiAgentOrchestratorPort
from app.infrastructure.legacy_multi_agent_adapter import (
    LegacyMultiAgentAdapter,
    build_multi_agent_orchestrator,
)


def test_adapter_implements_port() -> None:
    adapter = LegacyMultiAgentAdapter()
    assert isinstance(adapter, MultiAgentOrchestratorPort)


def test_build_multi_agent_orchestrator_returns_port() -> None:
    adapter = build_multi_agent_orchestrator()
    assert isinstance(adapter, MultiAgentOrchestratorPort)


def test_adapter_run_delegates_to_legacy() -> None:
    adapter = LegacyMultiAgentAdapter()
    with patch(
        "app.infrastructure.legacy_multi_agent_adapter._legacy_run",
        return_value={"status": "completed", "final_answer": "ok"},
    ) as mock_run:
        result = adapter.run("question", model="gpt-4", parallel=True)
    assert result == {"status": "completed", "final_answer": "ok"}
    mock_run.assert_called_once_with(
        "question", model="gpt-4", parallel=True,
        resume_request_id=None, enable_thinking=False,
    )


def test_adapter_run_streaming_delegates_to_legacy() -> None:
    adapter = LegacyMultiAgentAdapter()
    on_event = MagicMock()
    with patch(
        "app.infrastructure.legacy_multi_agent_adapter._legacy_streaming",
        return_value={"status": "completed", "final_answer": "ok"},
    ) as mock_streaming:
        result = adapter.run_streaming(
            "question", model="gpt-4", on_event=on_event,
        )
    assert result == {"status": "completed", "final_answer": "ok"}
    mock_streaming.assert_called_once_with(
        "question", model="gpt-4", parallel=False,
        resume_request_id=None, enable_thinking=False, on_event=on_event,
    )
