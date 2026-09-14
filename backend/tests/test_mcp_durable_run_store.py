from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.application.mcp_orchestration import MultiAgentMCPAdapter
from app.domain.ports import MCPOrchestrationRequest
from app.infrastructure.persistence.mcp_run_store import MCPDurableRunStore


def test_durable_run_store_persists_and_rehydrates_checkpoint(tmp_path) -> None:
    path = tmp_path / "mcp-runs.db"
    store = MCPDurableRunStore(path)
    created = store.create("run-1")
    assert created.state == "pending"

    running = store.transition(
        "run-1",
        "running",
        phase="worker",
        checkpoint="workers_running",
    )
    assert running.phase == "worker"

    rehydrated = MCPDurableRunStore(path).get("run-1")
    assert rehydrated is not None
    assert rehydrated.state == "running"
    assert rehydrated.checkpoint == "workers_running"


def test_durable_run_store_persists_request_fingerprint(tmp_path) -> None:
    path = tmp_path / "mcp-runs.db"
    store = MCPDurableRunStore(path)
    store.create("run-1", request_fingerprint="fp-1")
    restored = MCPDurableRunStore(path).get("run-1")
    assert restored is not None
    assert restored.request_fingerprint == "fp-1"


def test_durable_run_store_rejects_invalid_resume_transition(tmp_path) -> None:
    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    store.create("run-1")
    with pytest.raises(ValueError, match="invalid lifecycle transition"):
        store.transition("run-1", "completed")


def test_durable_run_store_keeps_worker_failure_for_resume(tmp_path) -> None:
    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    store.create("run-1")
    store.transition("run-1", "running")
    failed = store.transition(
        "run-1",
        "partial_success",
        phase="worker",
        checkpoint="workers_running",
        failure_phase="worker",
        worker_errors=({"worker_id": "w1", "code": "timeout"},),
        retry_count=1,
    )
    assert failed.final_status == "partial_success"
    restored = store.get("run-1")
    assert restored is not None
    assert restored.worker_errors[0]["worker_id"] == "w1"
    assert restored.retry_count == 1


def test_partial_success_can_be_resumed(tmp_path) -> None:
    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    store.create("run-1")
    store.transition("run-1", "running")
    store.transition("run-1", "partial_success", phase="worker")
    resumed = store.transition("run-1", "running", retry_count=1)
    assert resumed.state == "running"
    assert resumed.retry_count == 1


def test_adapter_persists_completed_run_and_returns_durable_id(tmp_path) -> None:
    class FakeOrchestrator:
        def run(self, prompt, **kwargs):
            return {
                "answer": "ok",
                "lead": {"status": "completed"},
                "synthesis": {"status": "completed"},
            }

    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    result = MultiAgentMCPAdapter(FakeOrchestrator(), store).run(
        MCPOrchestrationRequest.from_values(prompt="hello")
    )
    assert result.status == "success"
    assert result.run_id is not None
    persisted = store.get(result.run_id)
    assert persisted is not None
    assert persisted.state == "completed"
    assert persisted.checkpoint == "completed"
    assert len(result.orchestration["durable_events"]) == 2
    assert result.orchestration["event_count"] == 2


def test_durable_run_store_preserves_event_order(tmp_path) -> None:
    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    store.create("run-1")
    store.append_event("run-1", {"event": "orchestrate.start"})
    store.append_event("run-1", {"event": "orchestrate.done"})
    assert [event["event"] for event in store.list_events("run-1")] == [
        "orchestrate.start",
        "orchestrate.done",
    ]


def test_durable_run_store_deduplicates_replayed_event_id(tmp_path) -> None:
    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    store.create("run-1")
    event = {"event_id": "evt-1", "event": "worker.completed", "worker_id": "w1"}
    store.append_event("run-1", event)
    store.append_event("run-1", {**event, "payload": "replayed"})
    assert store.list_events("run-1") == [event]


def test_adapter_records_streaming_events_under_durable_run(tmp_path) -> None:
    class StreamingOrchestrator:
        def run_streaming(self, prompt, **kwargs):
            kwargs["on_event"]("orchestrate.start", {"phase": "lead"})
            kwargs["on_event"]("orchestrate.done", {"phase": "synthesis"})
            return {
                "answer": "ok",
                "lead": {"status": "completed"},
                "workers": [{"id": "w1", "status": "failed"}],
                "synthesis": {"status": "completed"},
            }

    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    adapter = MultiAgentMCPAdapter(StreamingOrchestrator(), store)
    result = adapter.run(
        MCPOrchestrationRequest.from_values(prompt="hello"),
        on_event=lambda _kind, _event: None,
    )
    assert result.run_id is not None
    events = store.list_events(result.run_id)
    assert [event["event"] for event in events[:2]] == [
        "orchestrate.start",
        "orchestrate.done",
    ]
    persisted = store.get(result.run_id)
    assert persisted is not None
    assert persisted.checkpoint == "completed"


def test_adapter_rejects_resume_with_different_request_context(tmp_path) -> None:
    class FakeOrchestrator:
        def run(self, prompt, **kwargs):
            return {
                "answer": "ok",
                "lead": {"status": "completed"},
                "workers": [{"id": "w1", "status": "failed"}],
                "synthesis": {"status": "completed"},
            }

    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    adapter = MultiAgentMCPAdapter(FakeOrchestrator(), store)
    original = adapter.run(MCPOrchestrationRequest.from_values(prompt="hello", scope="lead"))
    assert original.run_id is not None
    with pytest.raises(ValueError, match="resume context mismatch"):
        adapter.run(
            MCPOrchestrationRequest.from_values(
                prompt="hello",
                scope="restricted",
                resume_request_id=original.run_id,
            )
        )


def test_adapter_resumes_same_context_and_increments_retry_count(tmp_path) -> None:
    class RetryingOrchestrator:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, prompt, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return {
                    "answer": "partial",
                    "lead": {"status": "completed"},
                    "workers": [{"id": "w1", "status": "failed"}],
                    "synthesis": {"status": "completed"},
                }
            return {
                "answer": "complete",
                "lead": {"status": "completed"},
                "workers": [{"id": "w1", "status": "completed"}],
                "synthesis": {"status": "completed"},
            }

    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    adapter = MultiAgentMCPAdapter(RetryingOrchestrator(), store)
    request = MCPOrchestrationRequest.from_values(prompt="hello", scope="lead")
    first = adapter.run(request)
    assert first.status == "partial_success"
    assert first.run_id is not None

    resumed = adapter.run(
        MCPOrchestrationRequest.from_values(
            prompt="hello",
            scope="lead",
            resume_request_id=first.run_id,
        )
    )
    assert resumed.status == "success"
    assert resumed.run_id == first.run_id
    persisted = store.get(first.run_id)
    assert persisted is not None
    assert persisted.retry_count == 1
    assert persisted.state == "completed"
    recovery_events = [
        event for event in store.list_events(first.run_id)
        if event["event"] == "checkpoint_recovered"
    ]
    assert recovery_events[0]["retry_count"] == 1


def test_adapter_persists_failure_without_masking_original_error(tmp_path) -> None:
    class FailingOrchestrator:
        def run(self, prompt, **kwargs):
            raise RuntimeError("worker backend unavailable")

    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    adapter = MultiAgentMCPAdapter(FailingOrchestrator(), store)
    with pytest.raises(RuntimeError, match="worker backend unavailable"):
        adapter.run(MCPOrchestrationRequest.from_values(prompt="hello"))

    with sqlite3.connect(store.path) as connection:
        runs = list(connection.execute("SELECT run_id FROM mcp_durable_runs").fetchall())
    assert len(runs) == 1
    persisted = store.get(runs[0][0])
    assert persisted is not None
    assert persisted.state == "failed"
    assert persisted.failure_phase == "lead"
    assert persisted.last_error == "worker backend unavailable"


def test_durable_run_store_cancels_active_run_and_records_event(tmp_path) -> None:
    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    store.create("run-1")
    store.transition("run-1", "running")
    cancelled = store.cancel("run-1", reason="client disconnected")
    assert cancelled.state == "cancelled"
    assert cancelled.last_error == "client disconnected"
    events = store.list_events("run-1")
    assert events[-1]["event"] == "run_cancelled"
    assert events[-1]["reason"] == "client disconnected"


def test_durable_run_store_rejects_cancel_of_terminal_run(tmp_path) -> None:
    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    store.create("run-1")
    store.transition("run-1", "running")
    store.transition("run-1", "completed")
    with pytest.raises(ValueError, match="invalid lifecycle transition"):
        store.cancel("run-1")


def test_adapter_exposes_cancel_and_notifies_observer(tmp_path) -> None:
    class FakeOrchestrator:
        def run(self, prompt, **kwargs):
            return {
                "answer": "partial",
                "lead": {"status": "completed"},
                "workers": [{"id": "w1", "status": "failed"}],
                "synthesis": {"status": "completed"},
            }

    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    adapter = MultiAgentMCPAdapter(FakeOrchestrator(), store)
    result = adapter.run(MCPOrchestrationRequest.from_values(prompt="hello"))
    observed: list[tuple[str, dict]] = []
    cancelled = adapter.cancel(
        result.run_id or "",
        reason="client disconnected",
        on_event=lambda kind, event: observed.append((kind, event)),
    )
    assert cancelled.state == "cancelled"
    assert observed[0][0] == "run_cancelled"
    assert observed[0][1]["parent_task_id"] == result.run_id


def test_adapter_reads_durable_run_without_reexecuting(tmp_path) -> None:
    class FakeOrchestrator:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, prompt, **kwargs):
            self.calls += 1
            return {
                "answer": "partial",
                "lead": {"status": "completed"},
                "workers": [{"id": "w1", "status": "failed"}],
                "synthesis": {"status": "completed"},
            }

    orchestrator = FakeOrchestrator()
    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    adapter = MultiAgentMCPAdapter(orchestrator, store)
    result = adapter.run(MCPOrchestrationRequest.from_values(prompt="hello"))
    snapshot = adapter.get_run(result.run_id or "")
    assert snapshot is not None
    assert snapshot["state"] == "partial_success"
    assert snapshot["checkpoint"] == "completed"
    assert snapshot["events"]
    assert orchestrator.calls == 1
    assert adapter.get_run("unknown-run") is None


def test_adapter_lists_durable_runs_with_state_filter(tmp_path) -> None:
    class FakeOrchestrator:
        def run(self, prompt, **kwargs):
            return {
                "answer": "partial",
                "lead": {"status": "completed"},
                "workers": [{"id": "w1", "status": "failed"}],
                "synthesis": {"status": "completed"},
            }

    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    adapter = MultiAgentMCPAdapter(FakeOrchestrator(), store)
    result = adapter.run(MCPOrchestrationRequest.from_values(prompt="hello"))
    runs = adapter.list_runs(state="partial_success")
    assert [run["run_id"] for run in runs] == [result.run_id]
    assert runs[0]["event_count"] == 3
    assert adapter.list_runs(state="completed") == []


def test_durable_run_store_prevents_concurrent_resume_with_active_lease(tmp_path) -> None:
    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    store.create("run-1")
    first = store.acquire_lease("run-1", "worker-a", ttl_seconds=60)
    assert first.lease_owner == "worker-a"
    with pytest.raises(ValueError, match="leased by another owner"):
        store.acquire_lease("run-1", "worker-b")
    released = store.release_lease("run-1", "worker-a")
    assert released.lease_owner is None
    assert store.acquire_lease("run-1", "worker-b").lease_owner == "worker-b"


def test_durable_run_store_allows_expired_lease_recovery(tmp_path) -> None:
    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    store.create("run-1")
    store.acquire_lease("run-1", "worker-a", ttl_seconds=1)
    current = store.get("run-1")
    assert current is not None
    expired = current.__class__(
        **{
            **current.__dict__,
            "lease_expires_at": datetime.now(UTC) - timedelta(seconds=1),
        }
    )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE mcp_durable_runs SET state_json = ? WHERE run_id = ?",
            (json.dumps(expired.as_snapshot()), "run-1"),
        )
    assert store.acquire_lease("run-1", "worker-b").lease_owner == "worker-b"


def test_durable_run_store_renews_only_for_current_owner(tmp_path) -> None:
    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    store.create("run-1")
    acquired = store.acquire_lease("run-1", "worker-a", ttl_seconds=1)
    renewed = store.renew_lease("run-1", "worker-a", ttl_seconds=120)
    assert renewed.lease_owner == "worker-a"
    assert renewed.lease_expires_at is not None
    assert renewed.lease_expires_at > acquired.lease_expires_at
    with pytest.raises(ValueError, match="leased by another owner"):
        store.renew_lease("run-1", "worker-b")


def test_durable_run_store_allows_only_one_concurrent_owner(tmp_path) -> None:
    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    store.create("run-1")

    def acquire(owner: str) -> str:
        try:
            store.acquire_lease("run-1", owner, ttl_seconds=60)
            return "acquired"
        except ValueError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(acquire, ("worker-a", "worker-b")))
    assert sorted(outcomes) == ["acquired", "rejected"]


def test_adapter_releases_lease_after_successful_run(tmp_path) -> None:
    class FakeOrchestrator:
        def run(self, prompt, **kwargs):
            return {
                "answer": "ok",
                "lead": {"status": "completed"},
                "synthesis": {"status": "completed"},
            }

    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    result = MultiAgentMCPAdapter(FakeOrchestrator(), store).run(
        MCPOrchestrationRequest.from_values(prompt="hello")
    )
    persisted = store.get(result.run_id or "")
    assert persisted is not None
    assert persisted.lease_owner is None
    assert persisted.lease_expires_at is None


def test_adapter_releases_lease_after_failed_run(tmp_path) -> None:
    class FailingOrchestrator:
        def run(self, prompt, **kwargs):
            raise RuntimeError("backend down")

    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    adapter = MultiAgentMCPAdapter(FailingOrchestrator(), store)
    with pytest.raises(RuntimeError, match="backend down"):
        adapter.run(MCPOrchestrationRequest.from_values(prompt="hello"))
    runs = store.list_runs(state="failed")
    assert len(runs) == 1
    assert runs[0].lease_owner is None
