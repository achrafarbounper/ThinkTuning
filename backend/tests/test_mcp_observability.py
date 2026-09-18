# project/tests/test_mcp_observability.py

"""Tests d'OBSERVABILITÉ MCP (MCP 2.3.0 — SCRUM-161).

Critères d'acceptation couverts, brique par brique :

1. **Volume** — ``mcp_tool_calls_total{tool}`` (par tool, cardinalité bornée) ;
2. **Latence** — ``mcp_request_latency_seconds{method}`` (histogramme →
   ``histogram_quantile`` pour p50/p95/p99 + quantiles in-process
   ``latency_quantiles()`` pour les surfaces sans PromQL) ;
3. **Erreurs par tool** — ``mcp_tool_errors_total{tool}`` ;
4. **Sessions actives** — ``mcp_sessions_active`` (``SessionTracker`` + guard
   du transport SSE : aucune session fuit à la fermeture du flux) ;
5. **Attente HITL** — ``mcp_runs_awaiting_approval`` (publiée par le sweeper) ;
6. **Reconnexions** — ``mcp_sse_reconnections_total{mode}``
   (``resume_token`` / ``after_sequence`` / ``last_event_id``) ;
7. **Rejets rate limit** — ``mcp_rate_limit_rejections_total`` (middleware
   ``/mcp``, enforceur per-client, quota d'ouverture SSE) ;
8. **Corrélation** — ``correlation_id`` propagé de ``initialize`` vers
   l'audit, les notifications, les erreurs et la réponse HTTP.

Les assertions portent sur le CONTRAT observable (séries Prometheus du
``REGISTRY`` par défaut, payloads du dashboard, entrées d'audit, en-têtes) et
non sur l'implémentation interne.

Aucun import lourd (ni torch, ni transformers) — le socle MCP reste léger.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.domain.entities.mcp import MCPScopeRole, MCPTool, MCPVersion
from app.domain.ports.mcp_ports import MCPSecurityScope
from app.infrastructure.mcp import mcp_metrics, mcp_server_sse, resume_cursor
from app.infrastructure.mcp.backpressure import (
    CODE_SSE_QUOTA,
    SCOPE_QUOTA,
    STATUS_TOO_MANY_REQUESTS,
    CapacityRejection,
)
from app.infrastructure.mcp.mcp_server import (
    InMemoryToolProvider,
    ToolError,
    resolve_correlation_id,
)
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.notifications.notification_service import NotificationService
from app.infrastructure.mcp.protocol import empty_input_schema
from app.infrastructure.mcp.run_sweeper import RunSweeper
from app.infrastructure.mcp.security.scope_enforcer import MCPScopeEnforcer
from app.infrastructure.persistence.audit_store import MCP_ACTIONS, reset_audit_store
from app.infrastructure.persistence.mcp_run_store import MCPDurableRunStore

_API_KEY = "test-mcp-observability-key"
_AUTH = {"X-API-Key": _API_KEY}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    """Valeur d'une série Prometheus (``0.0`` si la série n'existe pas encore)."""
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


def _chunks(gen) -> list[str]:
    """Draine un générateur asynchrone (même helper que test_mcp_sse_resume)."""

    async def _run() -> list[str]:
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


def _chunks_events(gen) -> list[tuple[str, dict[str, Any], int | None]]:
    """Draine un flux SSE et retourne ``[(event, data, id)]`` (parseur de test).

    Même convention que ``test_mcp_sse_resume._parse`` : la sentinelle
    ``[DONE]`` et les commentaires de garde ne produisent aucune entrée.
    """
    out: list[tuple[str, dict[str, Any], int | None]] = []
    for chunk in _chunks(gen):
        event = "message"
        data = ""
        sse_id: int | None = None
        for line in chunk.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip() or "message"
            elif line.startswith("data:"):
                data = line[len("data:"):].lstrip()
            elif line.startswith("id:"):
                sse_id = int(line[len("id:"):].strip())
        if not data or data == "[DONE]":
            continue
        out.append((event, json.loads(data), sse_id))
    return out


def _request(method: str, params: dict[str, Any] | None = None, request_id: Any = 1) -> str:
    return json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}
    )


def _ok_tool(name: str) -> MCPTool:
    """Tool de test qui réussit (aucun argument requis)."""

    def _handler(_arguments: dict[str, Any]) -> str:
        return f"{name}: ok"

    return MCPTool(
        name=name,
        description="Tool de test (observabilité)",
        input_schema=empty_input_schema(),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
        required_scope=MCPScopeRole.READ_ONLY,
        handler=_handler,
    )


def _failing_tool(name: str) -> MCPTool:
    """Tool de test qui échoue côté MÉTIER (``isError: true``, pas d'erreur JSON-RPC)."""

    def _handler(_arguments: dict[str, Any]) -> str:
        raise ToolError("dépendance externe injoignable")

    return MCPTool(
        name=name,
        description="Tool de test en échec (observabilité)",
        input_schema=empty_input_schema(),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True},
        required_scope=MCPScopeRole.READ_ONLY,
        handler=_handler,
    )


@pytest.fixture(autouse=True)
def _clean_observability(monkeypatch):
    """Environnement neutre : clé API déterministe + fenêtre de latence vide."""
    monkeypatch.setenv("API_KEY", _API_KEY)
    mcp_metrics.reset_latency_window()
    yield
    mcp_metrics.reset_latency_window()


@pytest.fixture
def sse_client() -> TestClient:
    """Mini-app FastAPI ne montant QUE le router MCP SSE (tests isolés)."""
    app = FastAPI()
    app.include_router(mcp_server_sse.router)
    return TestClient(app)


@pytest.fixture
def durable_store(tmp_path) -> MCPDurableRunStore:
    """Store de runs durables éphémère injecté dans le transport SSE."""
    store = MCPDurableRunStore(tmp_path / "mcp-runs.db")
    store.create("run-1")
    mcp_server_sse.configure_mcp_durable_run_store(store)
    yield store
    mcp_server_sse.configure_mcp_durable_run_store(None)




# ============================================================================
# 1. Volume & erreurs par tool
# ============================================================================


def test_record_tool_call_counts_volume_and_errors_per_tool() -> None:
    """1 appel nominal + 1 échec : volume = 2, erreurs = 1 (labels distincts)."""
    before_volume = _sample("mcp_tool_calls_total", {"tool": "orchestrate"})
    before_errors = _sample("mcp_tool_errors_total", {"tool": "orchestrate"})

    mcp_metrics.record_tool_call("orchestrate")
    mcp_metrics.record_tool_call("orchestrate", is_error=True)

    assert _sample("mcp_tool_calls_total", {"tool": "orchestrate"}) == before_volume + 2
    assert _sample("mcp_tool_errors_total", {"tool": "orchestrate"}) == before_errors + 1


def test_tool_labels_are_normalized_and_bounded() -> None:
    """Cardinalité BORNÉE : vide → ``unknown``, caractères invalides → ``_``, 64 max."""
    mcp_metrics.record_tool_call("")
    assert _sample("mcp_tool_calls_total", {"tool": "unknown"}) == 1.0

    # Séparateurs de chemin : jamais dans un nom de série (aucune injection).
    mcp_metrics.record_tool_call("../../etc/passwd")
    assert _sample("mcp_tool_calls_total", {"tool": ".._.._etc_passwd"}) == 1.0
    assert REGISTRY.get_sample_value("mcp_tool_calls_total", {"tool": "../../etc/passwd"}) is None

    # Nom pathologiquement long : tronqué (une seule série, pas de croissance).
    mcp_metrics.record_tool_call("x" * 200)
    assert _sample("mcp_tool_calls_total", {"tool": "x" * 64}) == 1.0
    assert REGISTRY.get_sample_value("mcp_tool_calls_total", {"tool": "x" * 200}) is None


def test_handle_text_counts_volume_and_error_per_tool() -> None:
    """Le serveur alimente volume ET erreurs par tool (tool de test en échec).

    Les compteurs Prometheus sont GLOBAUX (REGISTRY par défaut, cumulés sur
    toute la suite) : les assertions portent sur des DELTAS avant/après —
    ``flaky`` est aussi enregistré par d'autres fichiers de tests.
    """
    server = build_mcp_server(tool_provider=InMemoryToolProvider([_failing_tool("flaky")]))
    before_volume = _sample("mcp_tool_calls_total", {"tool": "flaky"})
    before_errors = _sample("mcp_tool_errors_total", {"tool": "flaky"})

    reply = json.loads(
        server.handle_text(
            _request("tools/call", {"name": "flaky", "arguments": {}}), client_id="client-obs"
        )
    )

    assert reply["result"]["isError"] is True
    assert _sample("mcp_tool_calls_total", {"tool": "flaky"}) == before_volume + 1
    assert _sample("mcp_tool_errors_total", {"tool": "flaky"}) == before_errors + 1


def test_handle_text_counts_nominal_call_without_error() -> None:
    """Un appel RÉUSSI incrémente le volume SANS toucher au compteur d'erreurs."""
    server = build_mcp_server(tool_provider=InMemoryToolProvider([_ok_tool("healthy")]))

    reply = json.loads(
        server.handle_text(
            _request("tools/call", {"name": "healthy", "arguments": {}}), client_id="client-obs"
        )
    )

    assert reply["result"]["isError"] is False
    assert _sample("mcp_tool_calls_total", {"tool": "healthy"}) == 1.0
    assert REGISTRY.get_sample_value("mcp_tool_errors_total", {"tool": "healthy"}) is None


def test_tool_call_outside_catalog_is_counted_under_its_own_label() -> None:
    """Un tool hors catalogue (erreur JSON-RPC) est compté EN ERREUR sous son nom."""
    server = build_mcp_server()

    reply = json.loads(
        server.handle_text(
            _request("tools/call", {"name": "absent_tool", "arguments": {}}), client_id="client-obs"
        )
    )

    assert reply["error"]["code"] == -32602
    assert _sample("mcp_tool_calls_total", {"tool": "absent_tool"}) == 1.0
    assert _sample("mcp_tool_errors_total", {"tool": "absent_tool"}) == 1.0



# ============================================================================
# 2. Latence (p50 / p95 / p99)
# ============================================================================


def test_latency_quantiles_report_p50_p95_p99() -> None:
    """Fenêtre glissante : quantiles interpolés, monotones, en millisecondes."""
    window = mcp_metrics.LatencyWindow()
    for value in (0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08, 0.09, 0.10):
        window.observe("tools/call", value)

    stats = window.quantiles()["tools/call"]

    assert stats["count"] == 10
    assert stats["p50_ms"] == pytest.approx(55.0, abs=0.5)
    assert stats["p95_ms"] == pytest.approx(95.5, abs=0.5)
    assert stats["p99_ms"] == pytest.approx(99.1, abs=0.5)
    assert stats["p50_ms"] <= stats["p95_ms"] <= stats["p99_ms"] <= stats["max_ms"] == 100.0


def test_latency_window_is_bounded_and_resettable() -> None:
    """Fenêtre bornée (mémoire) + méthodes exotiques ignorées au-delà du plafond."""
    window = mcp_metrics.LatencyWindow(window=3, max_methods=2)
    for value in (0.1, 0.2, 0.3, 0.4):
        window.observe("ping", value)
    assert window.quantiles()["ping"]["count"] == 3  # 0.1 évincé (deque bornée)

    window.observe("tools/list", 0.01)
    window.observe("prompts/get", 0.01)  # 3e méthode : plafond atteint → ignorée
    assert window.tracked_methods == 2

    window.reset()
    assert window.tracked_methods == 0


def test_record_request_latency_normalizes_exotic_method_labels() -> None:
    """Un identifiant de méthode arbitraire ne crée PAS de série (``unknown``).

    Compteur global cumulé : assertion sur DELTA (d'autres tests observent
    aussi la série ``unknown`` — parse errors, notifications…).
    """
    before = _sample("mcp_request_latency_seconds_count", {"method": "unknown"})
    mcp_metrics.record_request_latency("custom/exotic/method", 0.01)

    assert _sample("mcp_request_latency_seconds_count", {"method": "unknown"}) == before + 1
    assert REGISTRY.get_sample_value(
        "mcp_request_latency_seconds_count", {"method": "custom/exotic/method"}
    ) is None


def test_handle_text_observes_method_latency_histogram_and_window() -> None:
    """``initialize`` : la latence est observée (histogramme + fenêtre, en ms).

    Histogramme GLOBAL cumulé → DELTA ; la FENÊTRE de quantiles, elle, est
    réinitialisée par le fixture (``_clean_observability``) : le compte y est
    exactement 1 (seule observation de CE test).
    """
    server = build_mcp_server()
    before = _sample("mcp_request_latency_seconds_count", {"method": "initialize"})

    server.handle_text(_request("initialize", {}), client_id="client-latency")

    assert _sample("mcp_request_latency_seconds_count", {"method": "initialize"}) == before + 1
    stats = mcp_metrics.latency_quantiles()["initialize"]
    assert stats["count"] == 1
    assert stats["p50_ms"] >= 0.0


def test_latency_histogram_exposes_buckets_for_promql_quantiles() -> None:
    """Buckets + ``_sum``/``_count`` présents : ``histogram_quantile`` exploitable."""
    mcp_metrics.record_request_latency("ping", 0.03)

    assert _sample("mcp_request_latency_seconds_count", {"method": "ping"}) >= 1.0
    assert _sample("mcp_request_latency_seconds_sum", {"method": "ping"}) > 0.0
    # ``le`` = borne supérieure du bucket (Prometheus ajoute ``+Inf`` seul).
    assert _sample(
        "mcp_request_latency_seconds_bucket", {"method": "ping", "le": "+Inf"}
    ) >= 1.0
    assert _sample(
        "mcp_request_latency_seconds_bucket", {"method": "ping", "le": "0.05"}
    ) >= 1.0
    assert 0.005 in mcp_metrics.LATENCY_BUCKETS_SECONDS


# ============================================================================
# 3. Sessions actives
# ============================================================================


def test_session_tracker_updates_gauge_and_purges_idle_sessions(monkeypatch) -> None:
    """Acquiert/relâche met la jauge à jour ; le TTL purge les sessions fantômes.

    Horloge CONTRÔLÉE (``monotonic``) : la purge dépend d'un TTL **minimum de
    1 s** et d'un intervalle anti-rafale de 30 s — un vrai ``sleep`` rendrait
    le test lent et instable, l'horloge injectée teste le contrat exactement.
    """
    clock = {"now": 1_000.0}
    monkeypatch.setattr(mcp_metrics.time, "monotonic", lambda: clock["now"])
    tracker = mcp_metrics.SessionTracker(ttl_seconds=1.0)
    try:
        tracker.acquire("sess-1")
        tracker.acquire("sess-2")
        assert tracker.active == 2
        assert _sample("mcp_sessions_active") == 2.0

        tracker.release("sess-1")
        assert tracker.active == 1
        assert _sample("mcp_sessions_active") == 1.0

        # 40 s plus tard : passe de purge jouée (>= 30 s d'intervalle) et
        # ``sess-2`` (inactive depuis 40 s > TTL 1 s) évincée.
        clock["now"] += 40.0
        tracker.acquire("sess-3")
        assert tracker.active == 1
        assert _sample("mcp_sessions_active") == 1.0
    finally:
        tracker.reset()
    assert _sample("mcp_sessions_active") == 0.0


def test_session_guard_releases_session_when_stream_closes() -> None:
    """Le guard libère la session à la fermeture du flux (fin normale)."""
    observed: list[float] = []

    async def _source():
        observed.append(_sample("mcp_sessions_active"))
        yield "data: 1\n\n"
        yield "data: 2\n\n"

    async def _run() -> list[str]:
        mcp_metrics.SESSION_TRACKER.acquire("sess-guard")
        return [
            chunk
            async for chunk in mcp_server_sse._session_guard(
                _source(), session_id="sess-guard"
            )
        ]

    chunks = asyncio.run(_run())

    assert chunks == ["data: 1\n\n", "data: 2\n\n"]
    assert observed == [1.0]  # session comptée PENDANT le flux
    assert _sample("mcp_sessions_active") == 0.0  # libérée à la fermeture


def test_sse_endpoint_tracks_and_releases_session(sse_client: TestClient) -> None:
    """``POST /mcp/sse`` : session acquise puis libérée (aucune fuite)."""
    response = sse_client.post(
        "/mcp/sse",
        content=_request("initialize", {}),
        headers={**_AUTH, "Mcp-Session-Id": "sess-http-1", "X-Client-Id": "client-sse"},
    )

    assert response.status_code == 200
    assert "data: [DONE]" in response.text
    assert _sample("mcp_sessions_active") == 0.0
    assert mcp_metrics.SESSION_TRACKER.active == 0


# ============================================================================
# 4. Attente HITL (approbation humaine)
# ============================================================================


def test_sweeper_publishes_awaiting_approval_gauge(tmp_path) -> None:
    """Le sweeper alimente ``mcp_runs_awaiting_approval`` (sous-ensemble d'actifs)."""
    store = MCPDurableRunStore(tmp_path / "mcp-runs-hitl.db")
    store.create("run-hitl")
    # ``pending`` → ``running`` → ``awaiting_approval`` : la machine à états
    # n'autorise pas l'approbation depuis ``pending`` (transition invalide).
    store.transition("run-hitl", "running", phase="lead", checkpoint="lead_planned")
    store.transition(
        "run-hitl", "awaiting_approval", phase="lead", checkpoint="synthesis_running"
    )
    store.create("run-running")
    store.transition("run-running", "running", phase="worker", checkpoint="workers_running")
    sweeper = RunSweeper(store, stale_after_seconds=900, clock=lambda: datetime.now(UTC))

    report = sweeper.sweep_once()

    assert report.awaiting_approval == 1
    assert report.as_dict()["awaiting_approval"] == 1
    assert report.active == 2
    assert _sample("mcp_runs_awaiting_approval") == 1.0
    assert _sample("mcp_runs_active") == 2.0


# ============================================================================
# 5. Reconnexions SSE (mode de curseur)
# ============================================================================


def _replay_payload(**arguments: Any) -> dict[str, Any]:
    """Message ``orchestrate_events`` (replay) — même forme que le transport."""
    return {"params": {"arguments": {"replay": True, "stream": True, **arguments}}}


def _last_replay_error(parsed: list[tuple[str, dict[str, Any], int | None]]) -> str:
    return next(data.get("error_code", "") for event, data, _ in parsed if event == "replay.error")


def test_reconnection_by_resume_token_is_counted(durable_store) -> None:
    """Reconnexion par CURSEUR SIGNÉ → ``mode=resume_token``."""
    token = resume_cursor.encode_resume_token("run-1", 0)
    before = _sample("mcp_sse_reconnections_total", {"mode": "resume_token"})

    parsed = _chunks_events(mcp_server_sse._replay_durable_events(_replay_payload(resume_token=token)))

    assert [
        event for event, _, _ in parsed if event in {"replay_completed", "replay.error"}
    ], "le replay doit se terminer par un événement terminal"
    assert (
        _sample("mcp_sse_reconnections_total", {"mode": "resume_token"}) == before + 1
    )


def test_reconnection_by_after_sequence_is_counted(durable_store) -> None:
    """Reconnexion par ``after_sequence`` explicite → ``mode=after_sequence``."""
    before = _sample("mcp_sse_reconnections_total", {"mode": "after_sequence"})

    _chunks(
        mcp_server_sse._replay_durable_events(
            _replay_payload(run_id="run-1", after_sequence=0)
        )
    )

    assert _sample("mcp_sse_reconnections_total", {"mode": "after_sequence"}) == before + 1


def test_reconnection_by_last_event_id_header_is_counted(durable_store) -> None:
    """Reconnexion par en-tête SSE natif → ``mode=last_event_id``."""
    before = _sample("mcp_sse_reconnections_total", {"mode": "last_event_id"})

    _chunks(
        mcp_server_sse._replay_durable_events(
            _replay_payload(run_id="run-1"), last_event_id="1"
        )
    )

    assert _sample("mcp_sse_reconnections_total", {"mode": "last_event_id"}) == before + 1


def test_replay_without_cursor_is_not_a_reconnection(durable_store) -> None:
    """PREMIÈRE lecture d'un run (aucun curseur) : aucune reconnection comptée."""
    before = sum(
        _sample("mcp_sse_reconnections_total", {"mode": mode})
        for mode in ("resume_token", "after_sequence", "last_event_id")
    )

    _chunks(mcp_server_sse._replay_durable_events(_replay_payload(run_id="run-1")))

    after = sum(
        _sample("mcp_sse_reconnections_total", {"mode": mode})
        for mode in ("resume_token", "after_sequence", "last_event_id")
    )
    assert after == before


def test_invalid_cursor_is_not_counted_as_reconnection(durable_store) -> None:
    """Curseur INVALIDE (erreur explicite) : la reprise n'est pas honorée."""
    before = _sample("mcp_sse_reconnections_total", {"mode": "after_sequence"})

    parsed = _chunks_events(
        mcp_server_sse._replay_durable_events(
            _replay_payload(run_id="run-1", after_sequence="pas-un-entier")
        )
    )

    assert _last_replay_error(parsed) == "after_sequence_invalid"
    assert _sample("mcp_sse_reconnections_total", {"mode": "after_sequence"}) == before


def test_expired_resume_token_is_not_counted(durable_store, monkeypatch) -> None:
    """Token EXPIRÉ : erreur explicite ``resume_token_expired``, aucun comptage."""
    token = resume_cursor.encode_resume_token(
        "run-1", 1, issued_at=time.time() - 10 * 24 * 3600
    )
    before = _sample("mcp_sse_reconnections_total", {"mode": "resume_token"})

    parsed = _chunks_events(
        mcp_server_sse._replay_durable_events(_replay_payload(resume_token=token))
    )

    assert _last_replay_error(parsed) == "resume_token_expired"
    assert _sample("mcp_sse_reconnections_total", {"mode": "resume_token"}) == before

# ============================================================================
# 6. Rejets rate limit
# ============================================================================


class _FakeRequest:
    """Requête minimale pour le middleware (méthode, chemin, client, en-têtes)."""

    def __init__(self, path: str, *, method: str = "POST", host: str = "10.0.0.7") -> None:
        self.method = method
        self.url = type("_Url", (), {"path": path})()
        self.headers: dict[str, str] = {}
        self.client = type("_Client", (), {"host": host})()


def test_middleware_counts_rate_limit_rejection_for_mcp_transport(monkeypatch) -> None:
    """``POST /mcp/sse`` refusé par le quota → compteur MCP dédié +1."""
    from app.api.middlewares import rate_limit as rate_limit_middleware

    monkeypatch.setattr(rate_limit_middleware, "_consume_costly", lambda *_a, **_k: 7)
    before = _sample("mcp_rate_limit_rejections_total")

    wait = rate_limit_middleware._enforce_rate_limit(_FakeRequest("/mcp/sse"))

    assert wait == 7
    assert _sample("mcp_rate_limit_rejections_total") == before + 1
    assert _sample("mcp_security_rejections_total", {"reason": "rate_limit"}) >= 1.0


def test_middleware_leaves_other_groups_out_of_mcp_counter(monkeypatch) -> None:
    """Un refus d'une AUTRE route coûteuse n'alimente pas le compteur MCP."""
    from app.api.middlewares import rate_limit as rate_limit_middleware

    monkeypatch.setattr(rate_limit_middleware, "_consume_costly", lambda *_a, **_k: 3)
    before = _sample("mcp_rate_limit_rejections_total")

    wait = rate_limit_middleware._enforce_rate_limit(_FakeRequest("/api/v1/train"))

    assert wait == 3
    assert _sample("mcp_rate_limit_rejections_total") == before


def test_scope_enforcer_counts_per_client_rate_limit_rejection() -> None:
    """L'enforceur MCP (débit per-client) alimente le MÊME compteur dédié."""
    scope = MCPSecurityScope(
        client_id="client-rate",
        role=MCPScopeRole.CONTRIBUTOR,
        rate_limit_per_minute=1,
    )
    enforcer = MCPScopeEnforcer(scope_resolver=lambda client_id: scope, clock=lambda: 0.0)
    before = _sample("mcp_rate_limit_rejections_total")

    enforcer.check_rate_limit("client-rate")  # 1er appel : dans le quota
    with pytest.raises(Exception) as excinfo:
        enforcer.check_rate_limit("client-rate")

    assert "Rate limit" in str(excinfo.value)
    assert _sample("mcp_rate_limit_rejections_total") == before + 1


def test_sse_quota_rejection_counts_as_rate_limit_rejection() -> None:
    """Quota d'OUVERTURE SSE (``429``) compté dans le compteur dédié."""
    before = _sample("mcp_rate_limit_rejections_total")
    before_quota = _sample("mcp_sse_quota_rejections_total", {"reason": "rate"})
    rejection = CapacityRejection(
        scope=SCOPE_QUOTA,
        code=CODE_SSE_QUOTA,
        status_code=STATUS_TOO_MANY_REQUESTS,
        retry_after_seconds=5,
        message="Trop d'ouvertures de flux MCP pour ce client",
    )

    response = mcp_server_sse._rejection_response(rejection, session_id="sess-quota")

    assert response.status_code == STATUS_TOO_MANY_REQUESTS
    assert _sample("mcp_rate_limit_rejections_total") == before + 1
    assert _sample("mcp_sse_quota_rejections_total", {"reason": "rate"}) == before_quota + 1

# ============================================================================
# 7. Corrélation — initialize → audit → erreurs → réponse HTTP → notifications
# ============================================================================


class _RecordingEmailNotifier:
    """EmailNotifier de test qui enregistre les envois (sans réseau)."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, to_address: str, subject: str, body_text: str, **kwargs) -> bool:
        self.sent.append(
            {"to": to_address, "subject": subject, "body_text": body_text, **kwargs}
        )
        return True


class _RecordingSlackNotifier:
    """SlackNotifier de test qui enregistre les envois (sans réseau)."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def send(self, text: str, *, blocks: list[dict[str, Any]] | None = None) -> bool:
        self.sent.append({"text": text, "blocks": blocks})
        return True


@pytest.fixture
def real_audit(tmp_path, monkeypatch):
    """Store d'audit RÉEL isolé (même pattern que ``test_mcp_audit``)."""
    monkeypatch.setenv("AGENT_AUDIT_PATH", str(tmp_path / "agent_audit.db"))
    monkeypatch.setenv("API_KEY", _API_KEY)
    store = reset_audit_store()
    yield store
    reset_audit_store()  # le store suivant repartira de SON chemin


def _server_on_real_audit():
    """Serveur MCP branché sur le VRAI audit (``mcp_audit.audit_mcp_call``)."""
    from app.infrastructure.mcp.mcp_audit import audit_mcp_call

    return build_mcp_server(
        scope=MCPScopeRole.READ_ONLY,
        version=MCPVersion(major=2, minor=3, patch=0),
        audit=audit_mcp_call,
    )


def _audit_mcp_rows(store) -> list[dict]:
    """Entrées d'audit MCP de la base de test, en ordre d'appel."""
    rows = [item for item in store.query(limit=200)["items"] if item["action"] in MCP_ACTIONS]
    rows.reverse()  # query est DESC ; on raisonne en ordre d'appel
    return rows


def test_correlation_meta_wins_over_transport_header() -> None:
    """Priorité : ``params._meta.correlationId`` > en-tête transport > généré."""
    payload = {"params": {"_meta": {"correlationId": "meta-cid-1"}}}
    assert resolve_correlation_id(payload, provided="header-cid") == "meta-cid-1"
    # Sans meta : l'en-tête transport est utilisé tel quel (après sanitisation).
    assert resolve_correlation_id(None, provided="header-cid") == "header-cid"
    # Sans aucune source : un identifiant est GÉNÉRÉ (12 hex — contrat erreurs).
    assert re.fullmatch(r"[0-9a-f]{12}", resolve_correlation_id(None))


def test_correlation_id_is_sanitized_and_bounded() -> None:
    """Un identifiant fourni est nettoyé : ``[A-Za-z0-9._-]``, 64 max."""
    dirty = "a b/ç;DROP <script>"
    assert resolve_correlation_id(None, provided=dirty) == "abDROPscript"
    assert len(resolve_correlation_id(None, provided="x" * 300)) == 64


def test_initialize_carries_correlation_id_and_transport_agrees(sse_client: TestClient) -> None:
    """Le handshake ``initialize`` écho le cid dans ``_meta`` ; transport et
    serveur convergent sur la MÊME valeur (résolution déterministe)."""
    cid = "corr-init-01"
    request = _request("initialize", {"_meta": {"correlationId": cid}})
    resolved = resolve_correlation_id(json.loads(request))

    response = sse_client.post(
        "/mcp/sse",
        content=request,
        headers={**_AUTH, "Mcp-Session-Id": "sess-corr-1", "X-Client-Id": "client-corr"},
    )

    assert response.status_code == 200
    assert response.headers.get("x-correlation-id") == cid == resolved
    body = json.loads(response.text.split("data: ", 1)[1].splitlines()[0])
    assert body["result"]["_meta"]["correlationId"] == cid


def test_audit_entry_carries_correlation_id_from_handshake(real_audit) -> None:
    """Chaîne initialize → tools/call : l'entrée d'audit porte le MÊME cid.

    Le handshake ``initialize`` n'est PAS audité (méthode de catalogue) mais
    l'appel d'action qui suit, tagué avec le même ``correlationId`` côté
    client, est journalisé avec l'identifiant de corrélation du handshake.
    """
    server = _server_on_real_audit()
    cid = "corr-audit-01"
    handshake = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"_meta": {"correlationId": cid}},
        }
    )
    server.handle_text(handshake, client_id="client-corr")

    raw = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "mcp_version", "arguments": {}, "_meta": {"correlationId": cid}},
        }
    )
    server.handle_text(raw, client_id="client-corr")

    rows = _audit_mcp_rows(real_audit)
    assert len(rows) == 1  # initialize n'écrit rien — seul tools/call est audité
    assert rows[0]["detail"]["correlationId"] == cid
    assert rows[0]["detail"]["tool"] == "mcp_version"


def test_error_response_carries_correlation_id() -> None:
    """Une erreur de protocole projette le cid dans ``error.data.correlationId``."""
    server = build_mcp_server(
        scope=MCPScopeRole.READ_ONLY, version=MCPVersion(major=2, minor=3, patch=0)
    )
    raw = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/bogus",
            "params": {"_meta": {"correlationId": "corr-err-01"}},
        }
    )
    response = json.loads(server.handle_text(raw, client_id="client-corr"))

    assert response["error"]["data"]["correlationId"] == "corr-err-01"


def test_sse_endpoint_echoes_transport_header(sse_client: TestClient) -> None:
    """L'en-tête ``X-Correlation-Id`` de la requête est écho dans la réponse."""
    response = sse_client.post(
        "/mcp/sse",
        content=_request("initialize", {}),
        headers={
            **_AUTH,
            "Mcp-Session-Id": "sess-corr-2",
            "X-Client-Id": "client-corr",
            "X-Correlation-Id": "hdr-cid-02",
        },
    )

    assert response.status_code == 200
    assert response.headers.get("x-correlation-id") == "hdr-cid-02"


def test_sse_endpoint_prefers_message_meta_over_header(sse_client: TestClient) -> None:
    """Un cid dans le MESSAGE bat l'en-tête transport (écho == valeur du message)."""
    response = sse_client.post(
        "/mcp/sse",
        content=_request("initialize", {"_meta": {"correlationId": "meta-cid-03"}}),
        headers={
            **_AUTH,
            "Mcp-Session-Id": "sess-corr-3",
            "X-Client-Id": "client-corr",
            "X-Correlation-Id": "hdr-cid-04",
        },
    )

    assert response.status_code == 200
    assert response.headers.get("x-correlation-id") == "meta-cid-03"


def test_notification_service_renders_correlation_id(caplog) -> None:
    """Le cid est normalisé puis rendu dans le texte, l'HTML, Slack et les logs."""
    email, slack = _RecordingEmailNotifier(), _RecordingSlackNotifier()
    service = NotificationService(
        email_notifier=email,
        slack_notifier=slack,
        clients_provider=lambda: [{"client_id": "dev@corp.com", "revoked": False}],
    )

    with caplog.at_level(logging.INFO):
        results = service.notify_all_clients(
            subject="ThinkTuning MCP 2.3.0",
            breaking_changes=["Observabilité et corrélation"],
            migration_guide="docs/mcp/migration.md",
            correlation_id="corr notif/01!",
        )

    assert results == {"email": 1, "slack": 1}
    normalized = "corrnotif01"  # espaces, ``/`` et ``!`` retirés
    assert f"Correlation ID : {normalized}" in email.sent[0]["body_text"]
    assert f"Correlation ID : {normalized}" in email.sent[0]["body_html"]
    assert normalized in slack.sent[0]["text"]
    context_blocks = [b for b in slack.sent[0]["blocks"] if b["type"] == "context"]
    assert normalized in context_blocks[0]["elements"][0]["text"]
    # Log d'envoi : corrélation journalisée côté serveur
    assert f"correlation_id={normalized}" in caplog.text


def test_notification_service_without_correlation_id_stays_clean() -> None:
    """Sans cid : aucun marqueur de corrélation dans les messages (additivité)."""
    email, slack = _RecordingEmailNotifier(), _RecordingSlackNotifier()
    service = NotificationService(
        email_notifier=email,
        slack_notifier=slack,
        clients_provider=lambda: [{"client_id": "dev@corp.com", "revoked": False}],
    )

    results = service.notify_all_clients(
        subject="ThinkTuning MCP 2.3.0",
        breaking_changes=["Observabilité"],
        migration_guide="docs/mcp/migration.md",
    )

    assert results == {"email": 1, "slack": 1}
    assert "Correlation ID" not in email.sent[0]["body_text"]
    assert "Correlation ID" not in email.sent[0]["body_html"]
    context_blocks = [b for b in slack.sent[0]["blocks"] if b["type"] == "context"]
    assert "Correlation ID" not in context_blocks[0]["elements"][0]["text"]


def test_notification_correlation_parameter_wins_over_context() -> None:
    """Le paramètre ``correlation_id`` écrase un cid déjà présent dans le contexte."""
    email, slack = _RecordingEmailNotifier(), _RecordingSlackNotifier()
    service = NotificationService(
        email_notifier=email,
        slack_notifier=slack,
        clients_provider=lambda: [{"client_id": "dev@corp.com", "revoked": False}],
    )

    service.notify_all_clients(
        subject="ThinkTuning MCP 2.3.0",
        breaking_changes=["Observabilité"],
        migration_guide="docs/mcp/migration.md",
        extra_context={"correlationId": "ctx-cid"},
        correlation_id="param-cid",
    )

    assert "Correlation ID : param-cid" in email.sent[0]["body_text"]
    assert "ctx-cid" not in email.sent[0]["body_text"]
