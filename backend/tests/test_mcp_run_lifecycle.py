"""MCP 2.3.0 (SCRUM-163) — cycle de vie STANDARDISÉ des runs.

Statuts exposés : ``queued | running | waiting_for_approval | completed |
failed | cancelled | expired`` (dual-accept avec le vocabulaire interne
``pending`` / ``awaiting_approval`` — Schema Evolution, jamais de breaking).

Critères d'acceptation couverts :

1. **Tests de transition** — matrice FSM, aliases, projection canonique,
   monotonie des checkpoints, ``final_status`` / ``is_retryable`` ;
2. **Reprise après déconnexion** — replay incrémental ``after_sequence`` ;
3. **Annulation propre** — ``cancel`` one-shot, terminal, événement persisté ;
4. **Autorisation vérifiée** — tools de mutation invisibles en READ_ONLY ;
5. **Péremption** — sweeper → ``expired`` → ``runs/retry`` (run lié).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.application.mcp_orchestration import MultiAgentMCPAdapter
from app.domain.entities.mcp import MCPScopeRole, MCPVersion
from app.domain.ports import (
    MCPDurableRunState,
    MCPOrchestrationRequest,
    canonical_mcp_run_status,
    normalize_mcp_run_state,
)
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.run_sweeper import RunSweeper
from app.infrastructure.persistence.mcp_run_store import MCPDurableRunStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _NoopOrchestrator:
    """Orchestrateur minimal (jamais appelé par les tests de cycle de vie)."""

    def run(self, prompt, **kwargs):
        return {
            "answer": "ok",
            "lead": {"status": "completed"},
            "synthesis": {"status": "completed"},
        }


class _StreamingOrchestrator:
    """Émet deux événements streamés puis achève (replay après déconnexion)."""

    def run_streaming(self, prompt, **kwargs):
        kwargs["on_event"]("orchestrate.start", {"phase": "lead"})
        kwargs["on_event"](
            "orchestrate.worker.completed", {"phase": "worker", "worker_id": "w1"}
        )
        return {
            "answer": "ok",
            "lead": {"status": "completed"},
            "synthesis": {"status": "completed"},
        }


class _LifecyclePort:
    """Port d'orchestration factice (surface MCP : visibilité des tools)."""

    def get_run(self, run_id):
        return {"run_id": run_id, "state": "pending", "status": "queued", "events": []}

    def get_events(self, run_id, *, after_sequence=0):
        return []

    def list_runs(self, *, state=None, limit=50):
        return []

    def cancel(self, run_id, *, reason=None, on_event=None):
        raise AssertionError("cancel must never be called by a READ_ONLY client")

    def retry(self, run_id, *, reason=None):
        raise AssertionError("retry must never be called by a READ_ONLY client")


def _store(tmp_path) -> MCPDurableRunStore:
    return MCPDurableRunStore(tmp_path / "mcp-runs.db")


def _future(seconds: int = 1800) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


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


def _run_state(name: str) -> MCPDurableRunState:
    return MCPDurableRunState(run_id="r", state=name)


# ---------------------------------------------------------------------------
# 1. Tests de transition (FSM + aliases + projection canonique)
# ---------------------------------------------------------------------------


def test_standardized_vocabulary_is_dual_accepted() -> None:
    """Entrée standardisée → interne (dual-accept) ; inconnu → fail-closed."""
    assert normalize_mcp_run_state("queued") == "pending"
    assert normalize_mcp_run_state("waiting_for_approval") == "awaiting_approval"
    # Vocabulaire interne : idempotent.
    assert normalize_mcp_run_state("awaiting_approval") == "awaiting_approval"
    # Casse / espaces tolérés (valeurs de config).
    assert normalize_mcp_run_state("  QUEUED ") == "pending"
    with pytest.raises(ValueError, match="unknown MCP run state"):
        normalize_mcp_run_state("teleporting")


def test_canonical_status_projection_matches_mcp_230_lifecycle() -> None:
    """Projection interne → standardisé : le cycle de vie exposé au client."""
    assert canonical_mcp_run_status("pending") == "queued"
    assert canonical_mcp_run_status("running") == "running"
    assert canonical_mcp_run_status("awaiting_approval") == "waiting_for_approval"
    assert canonical_mcp_run_status("completed") == "completed"
    assert canonical_mcp_run_status("failed") == "failed"
    assert canonical_mcp_run_status("cancelled") == "cancelled"
    assert canonical_mcp_run_status("expired") == "expired"
    assert canonical_mcp_run_status("partial_success") == "partial_success"


def test_lifecycle_transition_matrix() -> None:
    run = MCPDurableRunState(run_id="r")
    running = run.transition("running")
    assert running.state == "running"

    # ``pending`` → terminal direct interdit (aucune phase n'a démarré).
    with pytest.raises(ValueError, match="invalid lifecycle transition"):
        run.transition("completed")

    # ``running`` → tous les aboutissements (y compris ``expired``).
    for target in (
        "awaiting_approval",
        "partial_success",
        "completed",
        "failed",
        "cancelled",
        "expired",
    ):
        assert running.transition(target).state == target

    # Resume ciblé : ``awaiting_approval`` → ``running`` (même run_id).
    waiting = running.transition("awaiting_approval")
    resumed = waiting.transition("running", retry_count=1)
    assert resumed.state == "running"
    assert resumed.retry_count == 1

    # Terminaux : aucune transition sortante (``expired`` inclus).
    for terminal in ("completed", "failed", "cancelled", "expired"):
        frozen = running.transition(terminal)
        assert frozen.is_terminal
        with pytest.raises(ValueError, match="invalid lifecycle transition"):
            frozen.transition("running")


def test_store_transition_accepts_standardized_aliases(tmp_path) -> None:
    """Le store durable accepte le vocabulaire standardisé EN ENTRÉE."""
    store = _store(tmp_path)
    store.create("run-1")
    store.transition("run-1", "running")
    waiting = store.transition("run-1", "waiting_for_approval")
    assert waiting.state == "awaiting_approval"
    expired = store.transition("run-1", "expired")
    assert expired.state == "expired"


def test_checkpoint_is_monotonic() -> None:
    """Un événement tardif ne fait JAMAIS régresser le checkpoint durable."""
    running = MCPDurableRunState(run_id="r").transition("running")
    at_synthesis = running.transition(
        "running", phase="synthesis", checkpoint="synthesis_running"
    )
    late_worker = at_synthesis.transition(
        "running", phase="worker", checkpoint="workers_running"
    )
    assert late_worker.checkpoint == "synthesis_running"


def test_final_status_and_retryability() -> None:
    assert _run_state("completed").final_status == "success"
    assert _run_state("partial_success").final_status == "partial_success"
    assert _run_state("failed").final_status == "failed"
    assert _run_state("cancelled").final_status == "failed"
    assert _run_state("expired").final_status == "failed"
    assert _run_state("awaiting_approval").final_status == "awaiting_approval"

    assert _run_state("failed").is_retryable
    assert _run_state("expired").is_retryable
    assert not _run_state("completed").is_retryable
    assert not _run_state("cancelled").is_retryable
    assert _run_state("awaiting_approval").is_resumable


def test_store_roundtrips_parent_run_id(tmp_path) -> None:
    """La filiation ``parent_run_id`` survit à la réhydratation (SQLite)."""
    store = _store(tmp_path)
    store.create("child", parent_run_id="parent", retry_count=2)
    restored = MCPDurableRunStore(tmp_path / "mcp-runs.db").get("child")
    assert restored is not None
    assert restored.parent_run_id == "parent"
    assert restored.retry_count == 2


# ---------------------------------------------------------------------------
# 2. Reprise après déconnexion (replay incrémental)
# ---------------------------------------------------------------------------


def test_events_replay_after_disconnect(tmp_path) -> None:
    """Un client reconnecté rejoue UNIQUEMENT les événements postérieurs."""
    store = _store(tmp_path)
    adapter = MultiAgentMCPAdapter(_StreamingOrchestrator(), store)
    result = adapter.run(
        MCPOrchestrationRequest.from_values(prompt="hello"),
        on_event=lambda _kind, _event: None,
    )
    run_id = result.run_id
    assert run_id is not None

    all_events = adapter.get_events(run_id)
    assert len(all_events) >= 2

    # Le client coupe puis reconnecte avec son curseur ``after_sequence`` :
    # seuls les événements SÉQUENCE > curseur reviennent (jamais de doublon).
    cursor = all_events[0]["sequence"]
    resumed = adapter.get_events(run_id, after_sequence=cursor)
    assert [event["sequence"] for event in resumed] == [
        event["sequence"] for event in all_events[1:]
    ]


# ---------------------------------------------------------------------------
# 3. Annulation propre
# ---------------------------------------------------------------------------


def test_cancel_is_clean_and_one_shot(tmp_path) -> None:
    """``cancel`` : terminal, raison tracée, événement persisté, one-shot."""
    store = _store(tmp_path)
    adapter = MultiAgentMCPAdapter(_NoopOrchestrator(), store)
    store.create("run-1")
    store.transition("run-1", "running", phase="worker", checkpoint="workers_running")

    cancelled = adapter.cancel("run-1", reason="user stop")

    assert cancelled.state == "cancelled"
    assert cancelled.is_terminal
    assert cancelled.last_error == "user stop"
    assert [event["event"] for event in store.list_events("run-1")][-1] == "run_cancelled"

    # Une seconde annulation est refusée par la FSM (fail-closed, one-shot).
    with pytest.raises(ValueError, match="invalid lifecycle transition"):
        adapter.cancel("run-1")


# ---------------------------------------------------------------------------
# 4. Autorisation vérifiée (visibilité par rôle)
# ---------------------------------------------------------------------------


def test_lifecycle_mutation_tools_are_scope_gated() -> None:
    """Lecture (get/events) large ; mutation (cancel/retry) dès CONTRIBUTOR."""
    read_only = build_mcp_server(
        scope=MCPScopeRole.READ_ONLY,
        version=MCPVersion(major=2, minor=3, patch=0),
        orchestration_port=_LifecyclePort(),
        durable_run_tools=True,
    )
    ro_names = {
        tool["name"] for tool in _rpc(read_only, 1, "tools/list")["result"]["tools"]
    }
    assert "orchestrate_get_run" in ro_names
    assert "orchestrate_events" in ro_names
    assert "orchestrate_cancel" not in ro_names
    assert "orchestrate_retry" not in ro_names

    contributor = build_mcp_server(
        scope=MCPScopeRole.CONTRIBUTOR,
        version=MCPVersion(major=2, minor=3, patch=0),
        orchestration_port=_LifecyclePort(),
        durable_run_tools=True,
    )
    c_names = {
        tool["name"] for tool in _rpc(contributor, 1, "tools/list")["result"]["tools"]
    }
    assert {"orchestrate_cancel", "orchestrate_retry"} <= c_names


# ---------------------------------------------------------------------------
# 5. Péremption (sweeper → expired) + runs/retry (run lié)
# ---------------------------------------------------------------------------


def test_runs_get_exposes_canonical_status(tmp_path) -> None:
    """``runs/get`` / ``runs/list`` projettent ``status`` (vocabulaire 2.3.0)."""
    store = _store(tmp_path)
    adapter = MultiAgentMCPAdapter(_NoopOrchestrator(), store)
    store.create("run-q")

    queued = adapter.get_run("run-q")
    assert queued is not None
    assert queued["state"] == "pending"
    assert queued["status"] == "queued"

    store.transition("run-q", "running")
    store.transition("run-q", "awaiting_approval")
    waiting = adapter.get_run("run-q")
    assert waiting is not None
    assert waiting["state"] == "awaiting_approval"
    assert waiting["status"] == "waiting_for_approval"

    runs = adapter.list_runs()
    assert [run["status"] for run in runs] == ["waiting_for_approval"]


def test_sweeper_expiry_then_retry_creates_linked_run(tmp_path) -> None:
    """Récolte → ``expired`` (terminal) → ``retry`` crée un NOUVEAU run lié."""
    store = _store(tmp_path)
    adapter = MultiAgentMCPAdapter(_NoopOrchestrator(), store)
    store.create("run-1", request_fingerprint="fp-1")
    store.transition("run-1", "running", phase="worker", checkpoint="workers_running")

    sweeper = RunSweeper(store, stale_after_seconds=900, clock=lambda: _future())
    assert sweeper.sweep_once().stale_reaped == 1
    expired = store.get("run-1")
    assert expired is not None
    assert expired.state == "expired"

    retried = adapter.retry("run-1", reason="stale reaped")
    assert retried.run_id != "run-1"
    assert retried.state == "pending"
    assert retried.parent_run_id == "run-1"
    assert retried.retry_count == 1
    # Le prompt/scope d'origine est rejoué tel quel (même fingerprint).
    assert retried.request_fingerprint == "fp-1"

    # Le run SOURCE reste immuable et visible côté client (runs/get).
    source = adapter.get_run("run-1")
    assert source is not None
    assert source["state"] == "expired"
    assert source["status"] == "expired"

    # Traçabilité bilatérale, rejouable via runs/events.
    assert "run_retry" in [event["event"] for event in adapter.get_events("run-1")]
    assert "run_retry_scheduled" in [
        event["event"] for event in adapter.get_events(retried.run_id)
    ]

    # Fail-closed : un run non terminal / inconnu n'est JAMAIS réessayé.
    with pytest.raises(ValueError, match="not retryable"):
        adapter.retry(retried.run_id)
    with pytest.raises(KeyError):
        adapter.retry("unknown-run")


def test_retry_requires_durable_store() -> None:
    """Sans store durable : erreur explicite (jamais de retry fantôme)."""
    adapter = MultiAgentMCPAdapter(_NoopOrchestrator())
    with pytest.raises(RuntimeError, match="durable run store is required"):
        adapter.retry("run-1")
