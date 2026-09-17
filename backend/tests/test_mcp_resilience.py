"""Tests de résilience MCP (L2 — SCRUM-153) — preuves du tableau AliveMCP (L4 — SCRUM-155).

Ce module couvre les cinq briques de résilience livrées par L2 et documentées
dans ``docs/mcp/PATTERN_ALIVEMCP.md`` :

1. ``backpressure``  : admission bornée (global / client / quota) — ``503``/``429`` ;
2. ``idempotency``   : ``Idempotency-Key`` (new / replay / inflight / conflict) ;
3. ``mcp_events``    : politique d'événements — SOURCE UNIQUE (SSE + tool) ;
4. ``mcp_metrics``   : observabilité à cardinalité BORNÉE (jamais d'exception) ;
5. ``run_sweeper``   : réconciliation des runs durables zombies (stale / lease).

Les tests valident le CONTRAT (codes, en-têtes, invariants) plutôt que
l'implémentation : ce sont ces garanties qui sont référencées dans la
documentation et opposables au runtime.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from prometheus_client import REGISTRY

from app.domain.ports.mcp_ports import MCPDurableRunState
from app.infrastructure.mcp import mcp_metrics
from app.infrastructure.mcp.backpressure import (
    CODE_BACKPRESSURE,
    CODE_SSE_QUOTA,
    SCOPE_CLIENT,
    SCOPE_GLOBAL,
    SCOPE_QUOTA,
    STATUS_SERVICE_UNAVAILABLE,
    STATUS_TOO_MANY_REQUESTS,
    BackpressureGate,
    CapacityRejection,
    configure_backpressure_gate,
    get_backpressure_gate,
)
from app.infrastructure.mcp.idempotency import (
    IDEMPOTENCY_KEY_HEADER,
    OUTCOME_CONFLICT,
    OUTCOME_INFLIGHT,
    OUTCOME_NEW,
    OUTCOME_REPLAY,
    IdempotencyStore,
    configure_idempotency_store,
    extract_idempotency_key,
    fingerprint_payload,
    get_idempotency_store,
)
from app.infrastructure.mcp.mcp_events import (
    DEGRADATION_BACKPRESSURE,
    DEGRADATION_STALE_RUN,
    EVENT_DEGRADED,
    EVENT_DONE,
    EVENT_STARTED,
    EVENT_THINKING,
    EVENT_WORKER,
    GRANULARITY_MINIMAL,
    GRANULARITY_SUMMARY,
    GRANULARITY_VERBOSE,
    RUN_STATUS_AWAITING_APPROVAL,
    RUN_STATUS_FAILED,
    RUN_STATUS_IN_PROGRESS,
    RUN_STATUS_PARTIAL_SUCCESS,
    RUN_STATUS_SUCCESS,
    build_meta,
    degraded_meta,
    event_allowed_for_sse,
    event_allowed_for_tool,
    is_terminal_event,
    normalize_granularity,
    status_to_run_metric_label,
)
from app.infrastructure.mcp.run_sweeper import ACTION_STALE_RUN_REAPED, RunSweeper
from app.infrastructure.persistence.mcp_run_store import MCPDurableRunStore

# ---------------------------------------------------------------------------
# 1. Backpressure — admission bornée
# ---------------------------------------------------------------------------


def test_backpressure_global_capacity_rejects_with_503_and_retry_after() -> None:
    gate = BackpressureGate(max_concurrent=2, max_concurrent_per_client=2, open_rate=100)
    assert gate.try_acquire("a") is None
    assert gate.try_acquire("b") is None

    rejection = gate.try_acquire("c")

    assert isinstance(rejection, CapacityRejection)
    assert rejection.scope == SCOPE_GLOBAL
    assert rejection.status_code == STATUS_SERVICE_UNAVAILABLE
    assert rejection.code == CODE_BACKPRESSURE
    assert rejection.headers["Retry-After"] == "2"
    assert gate.active_streams == 2


def test_backpressure_per_client_quota_is_isolated() -> None:
    gate = BackpressureGate(max_concurrent=10, max_concurrent_per_client=1, open_rate=100)
    assert gate.try_acquire("noisy") is None

    rejection = gate.try_acquire("noisy")

    assert rejection is not None
    assert rejection.scope == SCOPE_CLIENT
    assert rejection.status_code == STATUS_SERVICE_UNAVAILABLE
    # Les autres clients conservent leur capacité (le bruit ne doit pas être global).
    assert gate.try_acquire("quiet") is None
    assert gate.active_streams_for("quiet") == 1


def test_backpressure_open_rate_is_429_not_503() -> None:
    gate = BackpressureGate(max_concurrent=10, max_concurrent_per_client=10, open_rate=1)
    assert gate.try_acquire("burst") is None

    rejection = gate.try_acquire("burst")

    assert rejection is not None
    assert rejection.scope == SCOPE_QUOTA
    assert rejection.status_code == STATUS_TOO_MANY_REQUESTS
    assert rejection.code == CODE_SSE_QUOTA
    assert rejection.reason == DEGRADATION_BACKPRESSURE


def test_backpressure_release_is_idempotent_and_purges_client_counter() -> None:
    gate = BackpressureGate(max_concurrent=1, max_concurrent_per_client=1, open_rate=100)
    assert gate.try_acquire("solo") is None

    gate.release("solo")
    gate.release("solo")  # sur-libération : jamais de compteur négatif

    assert gate.active_streams == 0
    assert gate.active_streams_for("solo") == 0
    # La place libérée est réellement réutilisable (aucune fuite de sémaphore).
    assert gate.try_acquire("solo") is None


def test_backpressure_rejection_payload_is_an_explicit_contract() -> None:
    gate = BackpressureGate(max_concurrent=1, max_concurrent_per_client=1, open_rate=100)
    assert gate.try_acquire("a") is None
    rejection = gate.try_acquire("a")
    assert rejection is not None

    payload = rejection.as_error_payload()

    assert payload["error"]["scope"] == SCOPE_CLIENT
    assert payload["error"]["degraded"] is True
    assert payload["error"]["reason"] == DEGRADATION_BACKPRESSURE
    assert payload["error"]["retry_after"] >= 1


def test_backpressure_singleton_is_configurable_for_tests() -> None:
    injected = BackpressureGate(max_concurrent=3)
    try:
        configure_backpressure_gate(injected)
        assert get_backpressure_gate() is injected
    finally:
        configure_backpressure_gate(None)
    # Reprise paresseuse : une porte neuve est construite sans I/O.
    assert isinstance(get_backpressure_gate(), BackpressureGate)


# ---------------------------------------------------------------------------
# 2. Idempotence — contrat Idempotency-Key
# ---------------------------------------------------------------------------


def _payload(**arguments: object) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "orchestrate", "arguments": arguments},
    }


def test_idempotency_new_then_inflight_never_duplicates_a_run() -> None:
    store = IdempotencyStore()

    first = store.reserve("key-1", "fp")
    assert first.outcome == OUTCOME_NEW
    assert first.should_execute is True

    second = store.reserve("key-1", "fp")
    assert second.outcome == OUTCOME_INFLIGHT
    assert second.is_in_flight is True
    assert second.should_execute is False


def test_idempotency_completed_key_replays_memorized_result() -> None:
    store = IdempotencyStore()
    store.reserve("key-1", "fp")
    store.complete("key-1", "fp", '{"answer":"ok"}')

    decision = store.reserve("key-1", "fp")

    assert decision.outcome == OUTCOME_REPLAY
    assert decision.is_replay is True
    assert decision.replay_result == '{"answer":"ok"}'
    assert decision.should_execute is False


def test_idempotency_same_key_different_fingerprint_is_a_conflict() -> None:
    store = IdempotencyStore()
    store.reserve("key-1", "fp-a")

    decision = store.reserve("key-1", "fp-b")

    assert decision.outcome == OUTCOME_CONFLICT
    assert decision.is_conflict is True


def test_idempotency_failed_execution_is_retryable() -> None:
    store = IdempotencyStore()
    store.reserve("key-1", "fp")

    assert store.release("key-1", "fp") is True

    assert store.reserve("key-1", "fp").outcome == OUTCOME_NEW


def test_idempotency_release_never_invalidates_a_memorized_success() -> None:
    store = IdempotencyStore()
    store.reserve("key-1", "fp")
    store.complete("key-1", "fp", "done")

    assert store.release("key-1", "fp") is False
    assert store.reserve("key-1", "fp").outcome == OUTCOME_REPLAY


def test_idempotency_without_key_preserves_execution_behaviour() -> None:
    store = IdempotencyStore()

    decision = store.reserve("", "fp")

    assert decision.outcome == OUTCOME_NEW
    assert decision.record is None
    assert store.size() == 0


def test_idempotency_store_is_bounded_lru() -> None:
    store = IdempotencyStore(max_entries=2)
    store.reserve("k1", "fp")
    store.reserve("k2", "fp")
    store.reserve("k3", "fp")

    assert store.size() == 2
    assert store.get("k1") is None
    assert store.get("k3") is not None


def test_idempotency_in_flight_reservation_expires_with_its_own_ttl() -> None:
    clock_value = 1_000.0
    store = IdempotencyStore(in_flight_ttl_seconds=120, clock=lambda: clock_value)
    store.reserve("key-1", "fp")

    clock_value += 121.0

    # L'exécution n'a jamais abouti (serveur tué) : la clé redevient tentable.
    assert store.reserve("key-1", "fp").outcome == OUTCOME_NEW


def test_idempotency_header_takes_precedence_over_json_argument() -> None:
    # Contrat public : en-tête HTTP standard (IETF draft), prioritaire.
    assert IDEMPOTENCY_KEY_HEADER == "Idempotency-Key"
    payload = _payload(idempotency_key="json-key")

    assert extract_idempotency_key(payload, "header-key") == "header-key"
    assert extract_idempotency_key(payload, None) == "json-key"
    assert extract_idempotency_key(payload, "   ") == "json-key"


def test_idempotency_key_is_bounded_and_never_a_resource_id() -> None:
    assert extract_idempotency_key(_payload(idempotency_key="x" * 500), None) is None
    assert extract_idempotency_key(_payload(idempotency_key="  "), None) is None
    # Aucune clé exploitable : le mode redevient non idempotent (jamais d'erreur).
    assert extract_idempotency_key({"jsonrpc": "2.0"}, None) is None
    assert extract_idempotency_key({"jsonrpc": "2.0", "params": {}}, None) is None
    assert extract_idempotency_key(_payload(), None) is None


def test_idempotency_fingerprint_ignores_the_key_but_detects_payload_change() -> None:
    with_key = _payload(idempotency_key="abc", prompt="hello")
    without_key = _payload(prompt="hello")
    other_prompt = _payload(idempotency_key="abc", prompt="bye")

    assert fingerprint_payload(with_key) == fingerprint_payload(without_key)
    assert fingerprint_payload(with_key) != fingerprint_payload(other_prompt)


def test_idempotency_fingerprint_is_stable_across_key_order() -> None:
    assert fingerprint_payload({"a": 1, "b": 2}) == fingerprint_payload({"b": 2, "a": 1})


def test_idempotency_singleton_is_configurable_for_tests() -> None:
    injected = IdempotencyStore()
    try:
        configure_idempotency_store(injected)
        assert get_idempotency_store() is injected
    finally:
        configure_idempotency_store(None)
    assert isinstance(get_idempotency_store(), IdempotencyStore)


# ---------------------------------------------------------------------------
# 3. Politique d'événements — source unique (SSE + tool)
# ---------------------------------------------------------------------------


TERMINAL_KINDS = ("orchestrate.done", "orchestrate.error", "message", "agent.done", "agent.error")


@pytest.mark.parametrize("kind", TERMINAL_KINDS)
@pytest.mark.parametrize(
    "granularity", [GRANULARITY_MINIMAL, GRANULARITY_SUMMARY, GRANULARITY_VERBOSE]
)
def test_terminal_events_are_never_filtered(kind: str, granularity: str) -> None:
    assert is_terminal_event(kind) is True
    assert event_allowed_for_sse(kind, granularity) is True
    assert event_allowed_for_tool(kind, granularity) is True


def test_sse_minimal_keeps_only_milestones() -> None:
    assert event_allowed_for_sse(EVENT_STARTED, GRANULARITY_MINIMAL) is True
    assert event_allowed_for_sse(EVENT_DONE, GRANULARITY_MINIMAL) is True
    assert event_allowed_for_sse(EVENT_THINKING, GRANULARITY_MINIMAL) is False
    assert event_allowed_for_sse("orchestrate.worker.start", GRANULARITY_MINIMAL) is False


def test_sse_summary_uses_whitelist_and_dynamic_prefixes() -> None:
    assert event_allowed_for_sse(EVENT_THINKING, GRANULARITY_SUMMARY) is True
    assert event_allowed_for_sse(EVENT_WORKER, GRANULARITY_SUMMARY) is True
    assert event_allowed_for_sse("orchestrate.worker.progress", GRANULARITY_SUMMARY) is True
    assert event_allowed_for_sse("orchestrate.synthesis.partial", GRANULARITY_SUMMARY) is True
    assert event_allowed_for_sse("orchestrate.unknown", GRANULARITY_SUMMARY) is False


def test_sse_verbose_lets_everything_through() -> None:
    assert event_allowed_for_sse("orchestrate.unknown", GRANULARITY_VERBOSE) is True


def test_tool_projection_filters_until_summary_then_passes_through() -> None:
    assert event_allowed_for_tool("orchestrate.worker.start", GRANULARITY_MINIMAL) is False
    assert event_allowed_for_tool("orchestrate.worker", GRANULARITY_SUMMARY) is True
    assert event_allowed_for_tool("orchestrate.unknown", GRANULARITY_SUMMARY) is False
    assert event_allowed_for_tool("orchestrate.unknown", GRANULARITY_VERBOSE) is True


def test_granularity_normalization_is_fail_safe() -> None:
    assert normalize_granularity(None) == GRANULARITY_SUMMARY
    assert normalize_granularity("  VERBOSE ") == GRANULARITY_VERBOSE
    assert normalize_granularity("nonsense") == GRANULARITY_SUMMARY


def test_build_meta_always_publishes_the_degradation_contract() -> None:
    nominal = build_meta(run_id="run-1")
    assert nominal == {
        "degraded": False,
        "run_id": "run-1",
        "failure_phase": None,
        "reason": None,
    }

    degraded = build_meta(run_id="run-1", reason=DEGRADATION_BACKPRESSURE, failure_phase="worker")
    assert degraded["degraded"] is True
    assert degraded["reason"] == DEGRADATION_BACKPRESSURE
    assert degraded["failure_phase"] == "worker"


def test_build_meta_rejects_unknown_failure_phase_but_never_overrides_contract() -> None:
    with pytest.raises(ValueError, match="failure_phase"):
        build_meta(failure_phase="dispatch")

    meta = build_meta(extra={"degraded": False, "run_id": "spoofed"})
    assert meta["degraded"] is False
    assert meta["run_id"] is None


def test_degraded_meta_is_an_explicit_degradation() -> None:
    meta = degraded_meta(DEGRADATION_STALE_RUN, run_id="run-1", failure_phase="synthesis")
    assert meta["degraded"] is True
    assert meta["reason"] == DEGRADATION_STALE_RUN


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("completed", RUN_STATUS_SUCCESS),
        ("partial_success", RUN_STATUS_PARTIAL_SUCCESS),
        ("budget_exhausted", RUN_STATUS_FAILED),
        ("rejected_loop", RUN_STATUS_FAILED),
        ("awaiting_approval", RUN_STATUS_AWAITING_APPROVAL),
        ("pending_approval", RUN_STATUS_AWAITING_APPROVAL),
        ("running", RUN_STATUS_IN_PROGRESS),
        ("", RUN_STATUS_IN_PROGRESS),
    ],
)
def test_status_projection_is_total_and_bounded(state: str, expected: str) -> None:
    assert status_to_run_metric_label(state) == expected


# ---------------------------------------------------------------------------
# 4. Métriques — observabilité défensive, cardinalité bornée
# ---------------------------------------------------------------------------


def test_metrics_record_functions_never_raise_on_unknown_labels() -> None:
    mcp_metrics.record_run_status("not-a-status")
    mcp_metrics.record_backpressure("not-a-scope")
    mcp_metrics.record_security_rejection("not-a-reason")
    mcp_metrics.record_idempotency("not-an-outcome")
    mcp_metrics.record_sweeper_action("not-an-action")
    mcp_metrics.record_degradation("")
    mcp_metrics.record_stream_interrupted("")
    mcp_metrics.record_sse_quota_rejection("")


def test_metrics_labels_are_bounded_to_known_values() -> None:
    before = REGISTRY.get_sample_value("mcp_runs_total", {"status": RUN_STATUS_IN_PROGRESS}) or 0.0

    mcp_metrics.record_run_status("not-a-status")

    after = REGISTRY.get_sample_value("mcp_runs_total", {"status": RUN_STATUS_IN_PROGRESS}) or 0.0
    assert after == before + 1
    # Aucune série parasite n'est créée par un label arbitraire.
    assert REGISTRY.get_sample_value("mcp_runs_total", {"status": "not-a-status"}) is None


def test_metrics_active_runs_gauge_is_clamped() -> None:
    mcp_metrics.set_active_runs(-5)
    assert REGISTRY.get_sample_value("mcp_runs_active") == 0.0


# ---------------------------------------------------------------------------
# 5. Sweeper — réconciliation des runs zombies
# ---------------------------------------------------------------------------


def _future(seconds: int = 3600) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


def _store(tmp_path) -> MCPDurableRunStore:
    return MCPDurableRunStore(tmp_path / "mcp-runs.db")


def test_sweeper_reaps_stale_running_run_and_publishes_degradation(tmp_path) -> None:
    store = _store(tmp_path)
    store.create("run-1")
    store.transition("run-1", "running", phase="worker", checkpoint="workers_running")
    sweeper = RunSweeper(store, stale_after_seconds=900, clock=lambda: _future())

    report = sweeper.sweep_once()

    assert report.scanned == 1
    assert report.stale_reaped == 1
    # ``active`` compte les runs NON aboutis observés AU MOMENT du scan (avant
    # récolte) : la jauge ``mcp_runs_active`` converge à la passe suivante.
    assert report.active == 1
    reaped = store.get("run-1")
    assert reaped is not None
    # MCP 2.3.0 : la péremption est un état TERMINAL DÉDIÉ — ni ``failed``
    # (échec métier) ni ``cancelled`` (décision client).
    assert reaped.state == "expired"
    assert reaped.last_error == ACTION_STALE_RUN_REAPED
    # La dégradation est persistée : rejouable et visible côté client.
    assert [event["event"] for event in store.list_events("run-1")][-1] == EVENT_DEGRADED


def test_sweeper_reaps_pending_run_as_expired(tmp_path) -> None:
    store = _store(tmp_path)
    store.create("run-1")
    sweeper = RunSweeper(store, stale_after_seconds=900, clock=lambda: _future())

    report = sweeper.sweep_once()

    assert report.stale_reaped == 1
    reaped = store.get("run-1")
    assert reaped is not None
    assert reaped.state == "expired"


def test_sweeper_never_reaps_settled_partial_success(tmp_path) -> None:
    store = _store(tmp_path)
    store.create("run-1")
    store.transition("run-1", "running")
    store.transition("run-1", "partial_success", phase="worker")
    sweeper = RunSweeper(store, stale_after_seconds=900, clock=lambda: _future())

    report = sweeper.sweep_once()

    assert report.stale_reaped == 0
    assert report.skipped == 1
    settled = store.get("run-1")
    assert settled is not None
    assert settled.state == "partial_success"


def test_sweeper_grants_a_longer_grace_to_human_approvals(tmp_path) -> None:
    store = _store(tmp_path)
    store.create("run-1")
    store.transition("run-1", "running")
    store.transition("run-1", "awaiting_approval", phase="worker")
    sweeper = RunSweeper(
        store,
        stale_after_seconds=900,
        awaiting_approval_grace_seconds=3600,
        clock=lambda: _future(1800),
    )

    assert sweeper.sweep_once().stale_reaped == 0
    pending = store.get("run-1")
    assert pending is not None
    assert pending.state == "awaiting_approval"

    sweeper.stale_after_seconds = 900
    sweeper.awaiting_approval_grace_seconds = 900
    sweeper._clock = lambda: _future(7200)  # horizon au-delà de la grâce
    assert sweeper.sweep_once().stale_reaped == 1
    reaped = store.get("run-1")
    assert reaped is not None
    assert reaped.state == "expired"


def test_sweeper_releases_expired_lease_without_failing(tmp_path) -> None:
    store = _store(tmp_path)
    store.create("run-1")
    store.transition("run-1", "running", phase="worker", checkpoint="workers_running")
    store.acquire_lease("run-1", "worker-a", ttl_seconds=1)
    sweeper = RunSweeper(store, stale_after_seconds=900, clock=lambda: _future())

    report = sweeper.sweep_once()

    assert report.leases_released == 1


def test_sweeper_survives_a_store_outage_without_raising() -> None:
    class BrokenStore:
        def list_runs(self, **kwargs: object) -> list[MCPDurableRunState]:
            raise RuntimeError("mongo down")

    sweeper = RunSweeper(BrokenStore(), clock=lambda: _future())  # type: ignore[arg-type]

    report = sweeper.sweep_once()

    assert report.errors == 1
    assert report.scanned == 0
    assert "mongo down" in report.error_details[0]


def test_sweeper_report_is_serializable_and_status_is_exposed(tmp_path) -> None:
    store = _store(tmp_path)
    sweeper = RunSweeper(store, clock=lambda: _future())

    report = sweeper.sweep_once()

    assert set(report.as_dict()) == {
        "at",
        "scanned",
        "stale_reaped",
        "leases_released",
        "active",
        "skipped",
        "errors",
        "error_details",
    }
    status = sweeper.status()
    assert status["running"] is False
    assert status["last_report"]["scanned"] == 0
