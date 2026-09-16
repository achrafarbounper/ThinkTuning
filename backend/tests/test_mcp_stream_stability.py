# project/tests/test_mcp_stream_stability.py
"""Stabilité de l'exécution durable et du streaming MCP (L1 — SCRUM-152).

Couvre les critères d'acceptation du lot L1 :

  * pont d'événements thread → asyncio RÉELLEMENT annulable (aucune fuite de
    thread consommateur sur heartbeat, aucun événement perdu après annulation) ;
  * normalisation ``phase`` / ``worker_id`` des événements durables (un
    événement worker ne retombe plus dans la phase « lead ») ;
  * ``checkpoint`` et ``last_sequence`` MONOTONES (état + store) ;
  * ``last_sequence`` mémorisé en base + replay incrémental ``list_events_after`` ;
  * ``parallel`` RÉELLEMENT effectif PAR REQUÊTE (dispatch + use cases) ;
  * ``orchestrate.started`` expose ``run_id``/``resumed``/``last_sequence`` et
    le worker exécute EXACTEMENT le run préparé ;
  * annulation propre sur Stop (aucun run zombie, y compris dès le prélude) ;
  * Flow Map complète en streaming (plan, workers, synthèse, approbations).
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

from app.application.mcp_orchestration import MultiAgentMCPAdapter
from app.domain.ports.mcp_ports import (
    MCPDurableRunState,
    MCPOrchestrationRequest,
    normalize_mcp_event,
)
from app.infrastructure.mcp import mcp_server_sse as sse
from app.infrastructure.persistence.mcp_run_store import MCPDurableRunStore

# --- Outillage commun (mêmes conventions que test_mcp_sse_done_always) --------


def _collect(gen) -> list[str]:
    async def _run() -> list[str]:
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


def _events(chunks: list[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for chunk in chunks:
        event = "message"
        data = ""
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip() or "message"
            elif line.startswith("data:"):
                data = line[len("data:"):].lstrip()
        if not data or data.startswith(":") or data == "[DONE]":
            continue
        out.append((event, data))
    return out


def _first_event(chunks: list[str], kind: str) -> dict[str, Any]:
    for name, data in _events(chunks):
        if name == kind:
            return json.loads(data)
    raise AssertionError(f"événement {kind!r} absent du flux")


def _stream_payload(prompt: str, request_id: Any = 1, **arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": "orchestrate",
            "arguments": {"prompt": prompt, "stream": True, **arguments},
        },
    }


class _ConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


def _freeze_orchestration_port(monkeypatch, port: Any = None) -> None:
    """Fige la résolution du port d'orchestration (restauré par monkeypatch)."""
    monkeypatch.setattr(sse, "_orchestration_port", port, raising=True)
    monkeypatch.setattr(sse, "_orchestration_port_resolved", True, raising=True)


class _FakeOrchestrationPort:
    """Port d'orchestration minimal : préparation + annulation enregistrées."""

    def __init__(self, snapshot: dict[str, Any] | None = None, error: str | None = None):
        self.snapshot = snapshot or {}
        self.error = error
        self.prepare_calls: list[MCPOrchestrationRequest] = []
        self.cancel_calls: list[dict[str, Any]] = []
        self.cancel_event = threading.Event()

    def prepare_run(self, request: MCPOrchestrationRequest) -> dict[str, Any] | None:
        self.prepare_calls.append(request)
        if self.error is not None:
            raise ValueError(self.error)
        return dict(self.snapshot) if self.snapshot else None

    def cancel(self, run_id: str, *, reason: str | None = None, on_event=None):
        self.cancel_calls.append({"run_id": run_id, "reason": reason})
        self.cancel_event.set()
        return MCPDurableRunState(run_id=str(run_id)).transition(
            "cancelled", last_error=reason
        )

    def run(self, request, *, on_event=None):  # pragma: no cover - non utilisé ici
        raise AssertionError("le transport SSE n'appelle jamais port.run()")

    def get_run(self, run_id): return None

    def get_events(self, run_id, *, after_sequence: int = 0): return []

    def list_runs(self, *, state=None, limit: int = 50): return []


# ============================================================================
# 1. Pont d'événements : annulable, sans perte, sans fuite de threads
# ============================================================================


def test_bridge_attente_annulable_sans_perte_ni_thread_bloque() -> None:
    """L'attente annulée ne consomme RIEN et ne bloque aucun thread.

    L'ancienne implémentation (``asyncio.to_thread(queue.get)``) laissait un
    thread consommateur bloqué par heartbeat : l'événement suivant était
    avalé par le fantôme et le thread ne s'arrêtait jamais.
    """

    async def _scenario() -> None:
        bridge = sse._SseEventBridge()
        threading.Thread(target=lambda: bridge.put(("a", {"v": 1})), daemon=True).start()
        assert await bridge.get(2.0) == ("a", {"v": 1})

        # Annulation d'une attente EN COURS : l'événement publié ensuite
        # revient intact au prochain get (aucune perte, aucun thread bloqué).
        pending = asyncio.ensure_future(bridge.get(5.0))
        await asyncio.sleep(0.02)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        threading.Thread(target=lambda: bridge.put(("b", {"v": 2})), daemon=True).start()
        assert await bridge.get(2.0) == ("b", {"v": 2})

        # Timeout → heartbeat SANS consommer la file ; la sentinelle close()
        # termine ensuite proprement la boucle (None).
        assert await bridge.get(0.01) is sse._HEARTBEAT
        bridge.close()
        assert await bridge.get(0.5) is None

    asyncio.run(_scenario())


def test_heartbeat_timeouts_ne_fuit_pas_de_thread_consommateur() -> None:
    """N heartbeats = N heartbeats, PAS N threads consommateurs orphelins."""

    async def _scenario() -> tuple[int, int]:
        bridge = sse._SseEventBridge()
        baseline = threading.active_count()
        beats = 0
        for _ in range(5):
            if await bridge.get(0.01) is sse._HEARTBEAT:
                beats += 1
        await asyncio.sleep(0.05)
        bridge.close()
        assert await bridge.get(0.2) is None
        return baseline, beats

    baseline, beats = asyncio.run(_scenario())
    assert beats == 5
    assert threading.active_count() <= baseline + 1


# ============================================================================
# 2. Normalisation des événements durables (phase / worker_id)
# ============================================================================


def test_normalize_mcp_event_classe_les_workers_et_derive_les_phases() -> None:
    """worker_id ⇒ phase « worker » ; phase inconnue dérivée, jamais perdue."""
    e1 = normalize_mcp_event({"event": "agent.worker.result", "worker_id": "w1"})
    assert e1["phase"] == "worker" and e1["worker_id"] == "w1"

    e2 = normalize_mcp_event({"event": "agent.plan"})
    assert e2["phase"] == "lead" and e2["worker_id"] is None

    e3 = normalize_mcp_event({"event": "agent.synthesis", "phase": "synthesis"})
    assert e3["phase"] == "synthesis"  # phase explicite respectée

    # Phase inconnue : dérivée de la hiérarchie au lieu du défaut « lead ».
    e4 = normalize_mcp_event({"event": "agent.dispatch", "phase": "dispatch", "worker_id": "w2"})
    assert e4["phase"] == "worker"
    e5 = normalize_mcp_event({"event": "agent.dispatch", "phase": "dispatch"})
    assert e5["phase"] == "lead"

    # worker_id vide ⇒ None (jamais la chaîne vide), phase dérivée du nom.
    e6 = normalize_mcp_event({"event": "agent.worker.start", "worker_id": "   "})
    assert e6["worker_id"] is None and e6["phase"] == "worker"


# ============================================================================
# 3. Checkpoint + last_sequence monotones (état + store)
# ============================================================================


def test_checkpoint_et_last_sequence_monotones(tmp_path) -> None:
    """Un événement tardif ne régresse JAMAIS la progression durable."""
    state = MCPDurableRunState(run_id="r1")
    state = state.transition("running", phase="lead", checkpoint="lead_planned")
    state = state.transition("running", phase="synthesis", checkpoint="synthesis_running")

    # Événement worker TARDIF : la phase suit, le checkpoint NON.
    regressed = state.transition("running", phase="worker", checkpoint="workers_running")
    assert regressed.phase == "worker"
    assert regressed.checkpoint == "synthesis_running"

    # last_sequence : max des séquences observées, jamais en arrière.
    advanced = regressed.transition("running", last_sequence=99)
    assert advanced.last_sequence == 99
    assert advanced.transition("running", last_sequence=5).last_sequence == 99

    # Le store applique la même règle en persistance.
    store = MCPDurableRunStore(tmp_path / "mono.db")
    store.create("run-mono")
    store.append_event("run-mono", {"event": "agent.plan", "event_id": "e1"})
    store.append_event("run-mono", {"event": "agent.synthesizing", "event_id": "e2"})
    store.transition("run-mono", "running", phase="synthesis", checkpoint="synthesis_running")
    # Un événement worker TARDIF persisté ne fait pas régresser le checkpoint.
    store.transition("run-mono", "running", phase="worker", checkpoint="workers_running")
    stored = store.get("run-mono")
    assert stored.checkpoint == "synthesis_running"
    assert stored.phase == "worker"
    assert stored.last_sequence == 2
    assert store.last_sequence("run-mono") == 2


def test_last_sequence_memorise_et_replay_incremental(tmp_path) -> None:
    """Séquence renvoyée par append_event, mémorisée, rejouable par curseur."""
    store = MCPDurableRunStore(tmp_path / "replay.db")
    store.create("run-replay")
    s1 = store.append_event("run-replay", {"event": "agent.plan", "event_id": "e1"})
    s2 = store.append_event(
        "run-replay", {"event": "agent.worker.start", "worker_id": "w1", "event_id": "e2"}
    )
    assert (s1, s2) == (1, 2)

    # Idempotence : re-pousser le même event_id conserve la séquence d'origine.
    again = store.append_event(
        "run-replay", {"event": "agent.worker.start", "worker_id": "w1", "event_id": "e2"}
    )
    assert again == 2

    # Le curseur est mémorisé sur le run (même transaction que l'événement).
    assert store.last_sequence("run-replay") == 2
    assert store.get("run-replay").as_snapshot()["last_sequence"] == 2

    # Un client coupé au 2e événement reprend SANS rejouer l'historique.
    assert store.list_events_after("run-replay", 2) == []
    store.append_event("run-replay", {"event": "agent.synthesizing", "event_id": "e3"})
    replay = store.list_events_after("run-replay", 2)
    assert [e["sequence"] for e in replay] == [3]
    assert replay[0]["event"] == "agent.synthesizing"


# ============================================================================
# 4. ``parallel`` effectif PAR REQUÊTE
# ============================================================================


def test_dispatch_parallel_est_effectif_par_requete(monkeypatch) -> None:
    """ThreadPoolExecutor utilisé SSI ``parallel=True`` pour CETTE requête."""
    from app.agent.legacy import orchestrator as orch_mod
    from app.agent.legacy.orchestrator import MultiAgentCoordinator
    from app.agent.legacy.plan_validator import PlanTask

    class _RecordingPool:
        count = 0

        def __init__(self, max_workers=None):
            type(self).count += 1

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def map(self, fn, iterable):
            return [fn(spec) for spec in list(iterable)]

    monkeypatch.setattr(orch_mod, "ThreadPoolExecutor", _RecordingPool)

    def _fake_worker(task, worker_prompt, on_event):
        return {"task_id": task.task_id, "role": task.role, "status": "ok", "duration_ms": 0.0}

    tasks = [PlanTask(f"t{i}", "Analyste", f"analyse {i}", []) for i in range(2)]
    coordinator = MultiAgentCoordinator(llm_client=object(), parallel=False)
    coordinator._run_worker = _fake_worker

    # Override explicite True : le pool EST instancié, ordre du plan conservé.
    _RecordingPool.count = 0
    workers, unexecuted = coordinator._dispatch(tasks, "prompt", None, parallel=True)
    assert _RecordingPool.count == 1
    assert not unexecuted
    assert [w["task_id"] for w in workers] == ["t0", "t1"]

    # Override explicite False + défaut None (→ False) : jamais de pool.
    _RecordingPool.count = 0
    coordinator._dispatch(tasks, "prompt", None, parallel=False)
    coordinator._dispatch(tasks, "prompt", None, parallel=None)
    assert _RecordingPool.count == 0


def test_ask_multi_agent_transmet_parallel_au_coordinateur(monkeypatch) -> None:
    """Les deux use cases (sync + streaming) forwardent ``parallel``."""
    from app.application import agent_cache

    class _FakeCoordinator:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def run(self, prompt, *, on_event=None, resume_request_id=None,
                enable_thinking=False, parallel=None):
            self.calls.append(
                {
                    "prompt": prompt,
                    "on_event": on_event,
                    "resume_request_id": resume_request_id,
                    "enable_thinking": enable_thinking,
                    "parallel": parallel,
                }
            )
            return {
                "status": "success",
                "final_answer": "ok",
                "plan": [],
                "workers": [],
                "unexecuted": [],
                "thinking": "",
                "usage": {},
            }

    fake = _FakeCoordinator()
    monkeypatch.setattr(agent_cache, "get_multi_agent_coordinator", lambda model=None: fake)

    agent_cache.ask_multi_agent("bonjour", parallel=True)
    assert fake.calls[-1]["parallel"] is True

    fake.calls.clear()
    agent_cache.ask_multi_agent_streaming(
        "bonjour", parallel=True, on_event=lambda kind, data: None
    )
    assert fake.calls[-1]["parallel"] is True
    assert fake.calls[-1]["on_event"] is not None


# ============================================================================
# 5. Streaming : run_id exposé dès ``orchestrate.started`` + annulation Stop
# ============================================================================


def test_stream_started_expose_le_run_prepare_et_le_worker_l_execute(monkeypatch) -> None:
    """started porte run_id/resumed/last_sequence ; le worker reprend CE run."""
    port = _FakeOrchestrationPort(snapshot={"run_id": "run-l1", "last_sequence": 7})
    _freeze_orchestration_port(monkeypatch, port)
    seen: dict[str, Any] = {}

    def _fake_multi(prompt: str, **kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        seen["prompt"] = prompt
        return {"answer": "ok"}

    monkeypatch.setattr(sse, "orchestrate_multi_agent", _fake_multi, raising=True)
    chunks = _collect(sse._stream_orchestrate(
        _stream_payload("bonjour", mode="multi_agent"),
        client_id="test", request=_ConnectedRequest()))
    assert chunks[-1] == "data: [DONE]\n\n"

    started = _first_event(chunks, "orchestrate.started")
    assert started == {
        "status": "started",
        "mode": "multi_agent",
        "run_id": "run-l1",
        "resumed": False,
        "last_sequence": 7,
    }
    assert len(port.prepare_calls) == 1  # préparation AVANT exécution
    assert seen["run_id"] == "run-l1"  # le run exposé EST celui exécuté


def test_stream_sans_port_durable_fonctionne_sans_run_id(monkeypatch) -> None:
    """Store durable indisponible : le flux reste fonctionnel (run_id absent)."""
    _freeze_orchestration_port(monkeypatch, None)

    def _fake_multi(prompt: str, **kwargs: Any) -> dict[str, Any]:
        return {"answer": "sans durable"}

    monkeypatch.setattr(sse, "orchestrate_multi_agent", _fake_multi, raising=True)
    chunks = _collect(sse._stream_orchestrate(
        _stream_payload("bonjour", mode="multi_agent"),
        client_id="test", request=_ConnectedRequest()))
    assert chunks[-1] == "data: [DONE]\n\n"
    started = _first_event(chunks, "orchestrate.started")
    assert started["run_id"] is None
    assert started["last_sequence"] == 0


def test_stream_prepare_run_inconnu_repond_32602_puis_done(monkeypatch) -> None:
    """``run_id`` inconnu : erreur de paramètres explicite, flux bien formé."""
    port = _FakeOrchestrationPort(error="unknown durable MCP run 'nope'")
    _freeze_orchestration_port(monkeypatch, port)
    chunks = _collect(sse._stream_orchestrate(
        _stream_payload("x", mode="multi_agent", run_id="nope"),
        client_id="test", request=_ConnectedRequest()))
    assert chunks[-1] == "data: [DONE]\n\n"
    error = _first_event(chunks, "orchestrate.error")
    assert error["error"]["code"] == -32602
    assert port.prepare_calls  # la préparation a bien été tentée


def test_stream_stop_des_le_prelude_annule_le_run_sans_zombie(monkeypatch) -> None:
    """Stop pendant ``orchestrate.started`` ⇒ cancel détaché (aucun zombie).

    Le prélude est un yield suspendu HORS de la boucle protégée : le contrat
    d'annulation doit s'appliquer ICI AUSSI, sinon un Stop juste après le
    démarrage laissait le run ``running`` à jamais (lease jamais libéré).
    """
    port = _FakeOrchestrationPort(snapshot={"run_id": "run-stop-1", "last_sequence": 3})
    _freeze_orchestration_port(monkeypatch, port)

    def _fake_multi(prompt: str, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
        raise AssertionError("le worker ne doit pas être attendu dans ce scénario")

    monkeypatch.setattr(sse, "orchestrate_multi_agent", _fake_multi, raising=True)

    async def _scenario() -> dict[str, Any]:
        gen = sse._stream_orchestrate(
            _stream_payload("bonjour", mode="multi_agent"),
            client_id="test", request=_ConnectedRequest())
        started = await gen.__anext__()  # suspendu sur le yield du prélude
        await gen.aclose()  # = bouton Stop du client
        return json.loads(started.splitlines()[1][len("data:"):])

    started = asyncio.run(_scenario())
    assert started["run_id"] == "run-stop-1"

    # L'annulation part dans un thread détaché : on attend le signal.
    assert port.cancel_event.wait(3.0), "le run durable doit être annulé au Stop"
    assert port.cancel_calls == [{"run_id": "run-stop-1", "reason": "client disconnected"}]


# ============================================================================
# 6. Adapter applicatif : classification, séquences, démarrage vs reprise
# ============================================================================


class _StubOrchestrator:
    """Orchestrateur applicatif stub (émet la trace minimale attendue)."""

    def run_streaming(self, prompt, *, model=None, parallel=False,
                      resume_request_id=None, enable_thinking=False,
                      on_event=None, **kwargs):
        if on_event is not None:
            on_event("agent.plan", {"plan": [{"role": "Analyste"}]})
            on_event("agent.worker.result", {"worker_id": "w1", "summary": "ok"})
        return {
            "status": "success",
            "final_answer": "fini",
            "plan": [],
            "workers": [],
            "unexecuted": [],
            "thinking": "",
            "events": [],
        }


def _request(prompt: str, **extra: Any) -> MCPOrchestrationRequest:
    return MCPOrchestrationRequest.from_values(prompt=prompt, session_id="s1", **extra)


def test_adapter_run_durable_classifie_sequence_et_ne_compte_pas_une_reprise(tmp_path) -> None:
    """Run préparé ``pending`` = DÉMARRAGE : retry_count 0, pas de recovered."""
    store = MCPDurableRunStore(tmp_path / "runs.db")
    adapter = MultiAgentMCPAdapter(_StubOrchestrator(), durable_store=store)

    prepared = adapter.prepare_run(_request("bonjour"))
    assert prepared is not None
    assert prepared["state"] == "pending"  # préparation SANS transition
    run_id = prepared["run_id"]

    streamed: list[tuple[str, dict[str, Any]]] = []
    result = adapter.run(
        _request("bonjour", run_id=run_id),
        on_event=lambda kind, data: streamed.append((kind, data)),
    )
    assert result.run_id == run_id
    assert result.status == "success"
    assert store.get(run_id).state == "completed"
    assert store.get(run_id).retry_count == 0

    # Les événements streamés portent leur SÉQUENCE durable (curseur replay).
    sequences = [data.get("sequence") for _, data in streamed]
    assert sequences[0] == 1 and sequences[1] == 2

    # Classification réparée (L1) : l'événement worker est bien « worker ».
    durable = store.list_events_after(run_id, 0)
    phases = {e["event"]: e["phase"] for e in durable}
    assert phases["agent.plan"] == "lead"
    assert phases["agent.worker.result"] == "worker"
    assert not any(e["event"] == "checkpoint_recovered" for e in durable)


def test_adapter_reprise_reelle_marque_checkpoint_recovered(tmp_path) -> None:
    """Un run déjà engagé repris sur le MÊME run_id : retry_count +1."""
    store = MCPDurableRunStore(tmp_path / "resume.db")
    adapter = MultiAgentMCPAdapter(_StubOrchestrator(), durable_store=store)
    store.create("run-int")
    store.transition("run-int", "running", phase="worker", checkpoint="workers_running")

    result = adapter.run(_request("bonjour", run_id="run-int"), on_event=lambda kind, data: None)
    assert result.run_id == "run-int"
    state = store.get("run-int")
    assert state.retry_count == 1
    assert state.state == "completed"

    durable = store.list_events_after("run-int", 0)
    recovered = [e for e in durable if e["event"] == "checkpoint_recovered"]
    assert recovered and recovered[0]["retry_count"] == 1


# ============================================================================
# 7. Flow Map streaming : session complète (plan, workers, approbations)
# ============================================================================


def test_stream_flowmap_session_complete_avec_approbation(monkeypatch) -> None:
    """La Flow Map reçoit la TRACE COMPLÈTE du run SSE (L1), HITL inclus."""
    from app.infrastructure.mcp import mcp_flow
    from app.infrastructure.persistence import flow_store as fs

    monkeypatch.setattr(mcp_flow, "_MCP_FLOW_ENABLED", True)
    fs.reset_flow_store()
    try:
        port = _FakeOrchestrationPort(snapshot={"run_id": "run-flow", "last_sequence": 0})
        _freeze_orchestration_port(monkeypatch, port)

        def _fake_multi(prompt: str, **kwargs: Any) -> dict[str, Any]:
            relay = kwargs["on_event"]
            relay("agent.plan", {"plan": [{"role": "Analyste"}]})
            relay("agent.worker.result", {"worker_id": "w1", "summary": "ok"})
            relay(
                "agent.worker.approval",
                {
                    "worker_id": "w2",
                    "approval": {"tool": "web_search"},
                    "message": "Policy : validation humaine requise",
                    "request_id": "req-9",
                },
            )
            return {"answer": "fini"}

        monkeypatch.setattr(sse, "orchestrate_multi_agent", _fake_multi, raising=True)
        chunks = _collect(sse._stream_orchestrate(
            _stream_payload("bonjour", mode="multi_agent"),
            client_id="test", request=_ConnectedRequest()))
        assert chunks[-1] == "data: [DONE]\n\n"

        flows = fs.get_flow_store().list(limit=50, status=None)
        assert flows, "la session Flow Map doit être créée par le chemin SSE"
        row = fs.get_flow_store().get(flows[0]["id"])
        assert row["source"] == "mcp"

        timeline = row.get("events") or []

        # Plan + workers tracés (auparavant : session vide hors thinking/tool).
        tool_events = [e for e in timeline if e.get("event") == "mcp.tool"]
        recorded = {str(e["data"].get("event")) for e in tool_events}
        assert "agent.plan" in recorded
        assert "agent.worker.result" in recorded

        # Approbation HITL tracée comme nœud d'approbation (et pas un outil).
        approval = next(
            e for e in timeline if e.get("event") == mcp_flow.MCP_APPROVAL
        )
        assert approval["data"]["approval"] == {"tool": "web_search"}
        assert approval["data"]["request_id"] == "req-9"
    finally:
        fs.reset_flow_store()
