# project/app/infrastructure/mcp/mcp_server_sse.py
"""Transport SSE (Server-Sent Events) du serveur MCP — ``POST /mcp/sse``.

Le client envoie un message JSON-RPC 2.0 dans le corps de la requête ; le
serveur répond en ``text/event-stream`` avec un unique événement ``message``
portant la réponse JSON-RPC (style « streamable HTTP » du protocole MCP
2025-06-18, sans dépendance à sse-starlette : la réponse est un flux SSE à
événement unique, construit à la main).

Conventions MCP respectées :
    - entête ``Mcp-Session-Id`` : écho de la session du client (S1 = serveur
      sans état : on renvoie l'identifiant reçu, ou on en génère un si absent) ;
    - notification JSON-RPC (pas d'``id``) : aucune réponse attendue — le flux
      SSE émet un commentaire de garde (``: ok``) pour rester bien formé ;
    - erreur de protocole : réponse JSON-RPC ``error`` (id: null) dans le flux ;
    - auth transport (P5) : ``X-API-Key`` exigée par défaut
      (``MCP_AUTH_REQUIRED``, fail-closed) — même clé, même repli dev et même
      comparaison à temps constant que la surface REST ; 401 en enveloppe v1.

Note strangler : l'endpoint MCP est déclaré ``include_in_schema=False`` — il
n'est PAS une route REST et n'a aucune raison d'apparaître dans
``openapi.json`` (source du client TypeScript, verrou
``test_aucune_route_hors_v1_montee``). MCP possède son propre discovery
(``initialize`` → ``capabilities``) : le transport SSE reste monté en
``/mcp/sse`` (voir docs/mcp/IMPLEMENTATION_PLAN.md) sans casser le contrat v1.

Rollback (docs/mcp/IMPLEMENTATION_PLAN.md) : si ``MCP_SERVER_ENABLED=false``,
toute requête reçoit ``503 Service Unavailable`` — le serveur MCP est désactivé
sans toucher au reste de l'API.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.responses import Response

from app.agent.settings import get_agent_config
from app.domain.entities.mcp import MCPScopeRole
from app.domain.ports.mcp_ports import MCPDurableRunStorePort, MCPOrchestrationPort
from app.infrastructure.mcp.mcp_audit import audit_mcp_call
from app.infrastructure.mcp.mcp_flow import (
    MCPCallContext,
    begin_orchestrate_flow,
    clear_call_context,
    set_call_context,
)
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.tools.orchestrate_tool import (
    _result_to_text,
    build_orchestration_request,
    orchestrate_multi_agent,
    orchestrate_stream,
    resolve_orchestration,
)
from app.infrastructure.persistence.audit_store import ACT_MCP_ORCHESTRATE
from app.infrastructure.security.api_key import is_valid_api_key

logger = logging.getLogger("thinktuning.mcp.sse")

# Noms d'événements terminaux : ils marquent TOUJOURS la réponse finale et
# ne sont JAMAIS filtrés ni abandonnés (ni granularité, ni disconnect
# transitoire) — invariants 2/3 de docs/mcp/MULTI_AGENT_SSE_FLOW.md.
_TERMINAL_SSE_KINDS = frozenset(
    {
        "orchestrate.done",
        "orchestrate.error",
        "message",
        "agent.done",
        "agent.error",
    }
)

router = APIRouter(prefix="/mcp", tags=["mcp"])

# Interrupteur de rollback (docs/mcp/IMPLEMENTATION_PLAN.md) : ``false``/``0``
# désactive le serveur MCP → 503 sur toutes les requêtes.
_MCP_SERVER_ENABLED = os.getenv("MCP_SERVER_ENABLED", "true").strip().lower() not in {
    "false",
    "0",
    "no",
    "off",
}

# Instance partagée du serveur (stateless, thread-safe). Le transport de
# l'Assistant IA utilise contributor afin de voir `orchestrate`; les mutations
# restent protégées par la policy du noyau et l'approbation humaine.
# Tâche 12 : le hook d'audit journalise chaque appel d'action MCP dans
# ``agent_audit`` (subject = client_id de l'en-tête ``X-Client-Id``).
# The Assistant IA uses the MCP orchestrator as its controlled entry point.
# CONTRIBUTOR is required to see `orchestrate`; mutations remain gated by the
# AgentCore sandbox policy and human approval.
_server = build_mcp_server(
    scope=MCPScopeRole.CONTRIBUTOR,
    audit=audit_mcp_call,
    durable_run_tools=True,
)
_durable_run_store: MCPDurableRunStorePort | None = None


def configure_mcp_durable_run_store(
    store: MCPDurableRunStorePort | None,
) -> None:
    """Injecte le store durable utilisé par le replay SSE.

    ``None`` restaure la résolution paresseuse du store MongoDB de production.
    """
    global _durable_run_store
    _durable_run_store = store


def get_mcp_durable_run_store() -> MCPDurableRunStorePort:
    """Résout le store durable SSE, avec MongoDB comme défaut production."""
    if _durable_run_store is not None:
        return _durable_run_store
    from app.infrastructure.persistence.mcp_mongo_run_store import (
        MongoMCPDurableRunStore,
    )

    return MongoMCPDurableRunStore()


# --- Orchestration durable : préparation du run + annulation -------------------
_orchestration_port: MCPOrchestrationPort | None = None
_orchestration_port_resolved = False


def configure_mcp_orchestration_port(port: MCPOrchestrationPort | None) -> None:
    """Injecte le port d'orchestration utilisé pour la préparation/annulation.

    ``None`` fige la résolution (aucun run durable sur ce transport) — les
    tests unitaires du streaming instancient ainsi un adapter sur un store
    dédié (SQLite/mongomock) sans jamais toucher la production.
    """
    global _orchestration_port, _orchestration_port_resolved
    _orchestration_port = port
    _orchestration_port_resolved = True


def get_mcp_orchestration_port() -> MCPOrchestrationPort | None:
    """Résout le port d'orchestration (Mongo en production, mémoïsé).

    ``None`` (store durable indisponible) laisse le streaming FONCTIONNEL :
    le flux part simplement sans ``run_id`` durable ni annulation persistée.
    """
    global _orchestration_port, _orchestration_port_resolved
    if _orchestration_port is not None:
        return _orchestration_port
    if _orchestration_port_resolved:
        return None
    try:
        from app.infrastructure.mcp.orchestration_factory import (
            build_mcp_orchestration_adapter,
        )

        _orchestration_port = build_mcp_orchestration_adapter()
    except Exception:
        logger.warning(
            "Port d'orchestration MCP indisponible : streaming sans run durable.",
            exc_info=True,
        )
        _orchestration_port = None
    _orchestration_port_resolved = True
    return _orchestration_port


def _cancel_durable_stream_run(run_id: str | None, *, reason: str) -> None:
    """Annule un run durable de façon DÉTACHÉE (jamais bloquante).

    Le bouton Stop ferme la connexion HTTP : la boucle SSE reçoit
    ``GeneratorExit``/``asyncio.CancelledError`` et ne peut plus ``await``.
    L'annulation part donc dans un thread daemon — sans annulation, le run
    restait ``running`` pour toujours (run zombie : plus aucun client ne le
    reprendra, et la reprise du même ``run_id`` restait bloquée par le lease).
    """
    if not run_id:
        return

    def _cancel() -> None:
        try:
            port = get_mcp_orchestration_port()
            if port is None:
                return
            port.cancel(run_id, reason=reason)
            logger.info(
                "Run durable MCP annulé : run_id=%s reason=%s",
                run_id,
                reason,
            )
        except Exception:
            # Un run déjà terminal (completed/failed/cancelled) n'est PAS une
            # anomalie : la transition est simplement refusée.
            logger.info(
                "Annulation du run durable MCP ignorée : run_id=%s reason=%s",
                run_id,
                reason,
            )

    threading.Thread(target=_cancel, name=f"mcp-cancel-{run_id}", daemon=True).start()


# --- Pont d'événements thread → boucle asyncio (annulable) ---------------------
_HEARTBEAT_TIMEOUT_SECONDS = 10.0
_HEARTBEAT = object()


class _SseEventBridge:
    """Pont réellement ANNULABLE entre le worker (thread) et le flux SSE.

    Le worker exécute du code SYNCHRONE (AgentCore / orchestrateur) : il publie
    ses événements depuis un thread. L'ancienne implémentation attendait
    ``asyncio.to_thread(events.get)`` — à chaque timeout de heartbeat, le thread
    consommateur restait BLOQUÉ sur ``queue.Queue.get()`` (fuite d'un thread par
    heartbeat, et aucune annulation possible). Ici la file appartient à la
    boucle (``asyncio.Queue``) et l'attente ``await bridge.get(timeout)`` est
    interrompue proprement : aucun thread orphelin, ``CancelledError`` honorée.
    """

    def __init__(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    def _put_nowait(self, item: Any) -> None:
        if self._closed:
            return
        self._queue.put_nowait(item)

    def put(self, item: Any) -> None:
        """Publie depuis le thread worker (thread-safe, jamais bloquant)."""
        if self._closed:
            return
        try:
            self._loop.call_soon_threadsafe(self._put_nowait, item)
        except RuntimeError:  # boucle fermée (client parti) : on abandonne
            self._closed = True

    def close(self) -> None:
        """Signale la fin du worker (sentinelle ``None``)."""
        if self._closed:
            return
        try:
            self._loop.call_soon_threadsafe(self._put_nowait, None)
        except RuntimeError:
            self._closed = True

    def drain_nowait(self) -> list[Any]:
        """Vide la file sans bloquer (drain du terminal sur déconnexion)."""
        drained: list[Any] = []
        while True:
            try:
                drained.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                return drained

    async def get(self, wait_seconds: float) -> Any:
        """Attente annulable ; renvoie ``_HEARTBEAT`` si le délai expire.

        ``asyncio.timeout`` (et non ``asyncio.wait_for`` sur un thread) : la
        coroutine d'attente est RÉELLEMENT annulée et retirée de la file — aucun
        thread ni tâche fantôme ne subsiste après un heartbeat.
        """
        try:
            async with asyncio.timeout(wait_seconds):
                return await self._queue.get()
        except TimeoutError:
            return _HEARTBEAT


def mcp_server_enabled() -> bool:
    """Le serveur MCP est-il activé ? (interrupteur de rollback)."""
    return _MCP_SERVER_ENABLED


def mcp_auth_required() -> bool:
    """Auth transport obligatoire sur ``POST /mcp/sse`` ? (P5 — défaut : oui).

    La surface MCP exécute des outils RÉELS : sans garde, ``MCP_FIRST=true``
    gèle l'HTTP legacy mais laisse un canal d'exécution ouvert. Lecture de
    l'environnement à l'appel (compatibilité ``monkeypatch.setenv`` des
    tests), puis le réglage persisté du module de configuration de l'IHM
    (``AgentConfig.mcp_auth_required`` — base MongoDB, cf. SCRUM-138) ; repli
    ``True`` (fail-closed) si la configuration n'est pas chargeable. Rollback
    explicite : ``MCP_AUTH_REQUIRED=false``.
    """
    env = os.getenv("MCP_AUTH_REQUIRED")
    if env is not None:
        return env.strip().lower() not in {"false", "0", "no", "off"}
    try:
        return get_agent_config().mcp_auth_required
    except Exception:
        return True


def _sse_message(payload: dict[str, Any] | str | None) -> str:
    """Sérialise une réponse JSON-RPC en événement SSE ``message``.

    ``handle_text`` retourne déjà le JSON-RPC SÉRIALISÉ (str) : il est émis
    brut après ``data:`` (jamais re-encodé comme chaîne JSON — sinon la réponse
    serait double-échappée pour le client MCP).
    """
    if payload is None:  # notification : commentaire de garde, pas de réponse
        return ": ok\n\n"
    if isinstance(payload, str):
        data = payload
    else:
        data = json.dumps(payload, ensure_ascii=False)
    return f"event: message\ndata: {data}\n\n"


def _sse_event(event: str, payload: dict[str, Any]) -> str:
    """Sérialise une progression MCP en événement SSE nommé."""
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _is_streaming_orchestrate(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("method") != "tools/call":
        return False
    params = payload.get("params")
    arguments = params.get("arguments") if isinstance(params, dict) else None
    return (
        isinstance(params, dict)
        and params.get("name") == "orchestrate"
        and isinstance(arguments, dict)
        and bool(arguments.get("stream") or arguments.get("enable_thinking"))
    )


def _is_durable_replay(payload: object) -> bool:
    if not isinstance(payload, dict) or payload.get("method") != "tools/call":
        return False
    params = payload.get("params")
    arguments = params.get("arguments") if isinstance(params, dict) else None
    return (
        isinstance(params, dict)
        and params.get("name") == "orchestrate_events"
        and isinstance(arguments, dict)
        and bool(arguments.get("stream") or arguments.get("replay"))
    )


async def _replay_durable_events(payload: dict[str, Any]) -> AsyncIterator[str]:
    arguments = dict((payload.get("params") or {}).get("arguments") or {})
    run_id = str(arguments.get("run_id") or "").strip()
    try:
        after_sequence = int(arguments.get("after_sequence", 0))
    except (TypeError, ValueError):
        yield _sse_event(
            "replay.error",
            {"run_id": run_id, "error": "after_sequence must be an integer"},
        )
        yield "data: [DONE]\n\n"
        return
    if not run_id:
        yield _sse_event("replay.error", {"run_id": "", "error": "run_id is required"})
        yield "data: [DONE]\n\n"
        return
    yield _sse_event("replay_started", {"run_id": run_id, "after_sequence": after_sequence})
    try:
        events = get_mcp_durable_run_store().list_events_after(run_id, after_sequence)
        for event in events:
            yield _sse_event("orchestrate.replay", event)
        yield _sse_event(
            "replay_completed",
            {
                "run_id": run_id,
                "last_sequence": (events[-1]["sequence"] if events else after_sequence),
            },
        )
    except Exception as exc:
        logger.exception("MCP durable event replay failed")
        yield _sse_event("replay.error", {"run_id": run_id, "error": str(exc)})
    yield "data: [DONE]\n\n"


async def _stream_orchestrate(
    payload: dict[str, Any],
    *,
    client_id: str,
    request: Request | None = None,
) -> AsyncIterator[str]:
    """Relaye la réflexion et la progression du tool MCP en temps réel.

    Les événements de progression reprennent les payloads du flux core
    (`thinking_delta` et `core_tool`). Les noms `orchestrate.*` restent
    conservés pour la compatibilité avec les clients MCP existants. Un appel
    qui active explicitement `enable_thinking` est automatiquement streamé,
    même si `stream` n'est pas fourni.
    """
    request_id = payload.get("id")
    params = payload.get("params") or {}
    arguments = dict(params.get("arguments") or {})
    arguments.pop("stream", None)
    try:
        # P0 (SCRUM-151) : décision d'orchestration MUTUALISÉE — le chemin
        # stream consomme exactement la même résolution que le handler du tool
        # (mode, garde MCP_MULTI_AGENT_ENABLED, WorkerScopePolicy, granularité,
        # découplage run_id / resume_request_id). Toute décision divergente
        # stream vs non-stream est dès lors impossible par construction.
        resolution = resolve_orchestration(arguments)
    except ValueError as exc:
        yield _sse_event(
            "orchestrate.error",
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": str(exc)},
            },
        )
        yield "data: [DONE]\n\n"
        return

    # --- Run durable PRÉPARÉ avant le premier octet (L1 — SCRUM-152) ---------
    # Le client doit connaître son ``run_id`` DÈS ``orchestrate.started`` pour
    # (a) afficher la traçabilité, (b) reprendre après coupure
    # (``orchestrate_events`` + ``after_sequence``), (c) annuler proprement.
    prompt = str(arguments.get("prompt") or "")
    durable_run_id: str | None = resolution.run_id
    durable_resumed = bool(resolution.run_id)
    last_sequence = 0
    if resolution.mode == "multi_agent":
        try:
            prepared = get_mcp_orchestration_port()
            if prepared is not None:
                prepared_snapshot = prepared.prepare_run(
                    build_orchestration_request(resolution, prompt)
                )
                if prepared_snapshot:
                    durable_run_id = str(prepared_snapshot.get("run_id") or "") or durable_run_id
                    durable_resumed = bool(prepared_snapshot.get("run_id")) and bool(
                        resolution.run_id
                    )
                    last_sequence = int(prepared_snapshot.get("last_sequence") or 0)
        except ValueError as exc:
            # ``run_id`` inconnu / terminal / empreinte divergente : erreur de
            # paramètres explicite (même code que la validation d'arguments).
            yield _sse_event(
                "orchestrate.error",
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": str(exc)},
                },
            )
            yield "data: [DONE]\n\n"
            return

    bridge = _SseEventBridge()
    disconnected = threading.Event()

    def emit(kind: str, data: dict[str, Any]) -> None:
        # Les événements terminaux portent la réponse finale : ils ne sont
        # JAMAIS abandonnés sur disconnect transitoire (le poll
        # ``is_disconnected()`` peut être vrai fugacement derrière un proxy ;
        # la boucle de lecture décidera seule d'interrompre le flux).
        if disconnected.is_set() and kind not in _TERMINAL_SSE_KINDS:
            return
        bridge.put((kind, data))

    def worker() -> None:
        # --- Flow Map MCP (chemin streaming) ----------------------------------
        # Ce chemin BYPASSE ``MCPServer._handle_method`` : le contexte d'appel
        # et la session riche sont posés ICI, DANS le thread worker (les
        # ContextVars ne traversent pas ``threading.Thread``). Le recorder est
        # ainsi visible du run (host sortant → ``current_recorder``) et la
        # session ``source="mcp"`` alimentée en temps réel. Toute erreur de
        # traçage reste NON bloquante (le flux SSE prime).
        flow_token: object | None = None
        recorder: Any = None
        try:
            flow_token = set_call_context(
                MCPCallContext(
                    client_id=client_id,
                    request_id=str(request_id) if request_id is not None else None,
                )
            )
            recorder = begin_orchestrate_flow(
                prompt=str(arguments.get("prompt") or ""),
                session_id=str(arguments.get("session_id") or "default"),
                scope=str(arguments.get("scope") or "default"),
                run_id=durable_run_id,
            )
        except Exception:  # pragma: no cover - le flow ne doit JAMAIS casser le flux
            recorder = None

        def relay(kind: str, data: dict[str, Any]) -> None:
            """Relie SSE (temps réel) et Flow Map (persistance) sans les coupler.

            L1 (SCRUM-152) : la Flow Map reçoit la TRACE COMPLÈTE du run en
            streaming (plan, workers, synthèse, approbations) — auparavant seuls
            ``thinking``/``tool`` étaient persistés, si bien qu'une session
            ouverte par le chemin SSE apparaissait vide dans la Flow Map, et les
            approbations HITL n'y figuraient jamais.
            """
            emit(kind, data)
            if recorder is None:
                return
            try:
                payload = dict(data or {})
                if kind == "orchestrate.thinking":
                    recorder.record_thinking(str(payload.get("thinking_delta") or ""))
                elif kind == "orchestrate.tool":
                    recorder.record_tool(payload)
                elif kind in {"agent.worker.approval", "orchestrate.approval"}:
                    raw_approval = payload.get("approval")
                    approval = raw_approval if isinstance(raw_approval, dict) else payload
                    recorder.record_approval(
                        tool=str(approval.get("tool") or payload.get("tool") or ""),
                        message=str(payload.get("message") or ""),
                        request_id=payload.get("request_id"),
                    )
                elif kind not in {"orchestrate.started", "orchestrate.done", "message"}:
                    # Plan / reprise / workers / synthèse / phases : tracés dans
                    # la timeline (mêmes noms d'événements que le chemin
                    # non-stream, cf. ``orchestrate_multi_agent``).
                    recorder.record_tool({"event": kind, **payload})
            except Exception:  # pragma: no cover - le flow ne casserait rien
                logger.debug("Flow MCP : relais d'événement ignoré (%s)", kind)

        try:
            result: Any
            approval_payload: dict[str, Any] | None = None
            approval_request_id: str | None = None
            if resolution.fallback is not None:
                # Repli mono-agent EXPLICITE : émis comme notice dédiée —
                # l'UI ne doit JAMAIS l'interpréter comme une réussite
                # multi-agent (invariant docs/mcp/MULTI_AGENT_SSE_FLOW.md §2.3).
                emit("orchestration_fallback", dict(resolution.fallback))
            if resolution.mode == "multi_agent":
                multi_agent_result = orchestrate_multi_agent(
                    prompt,
                    session_id=resolution.session_id,
                    scope=resolution.scope,
                    model=resolution.model,
                    parallel=resolution.parallel,
                    enable_thinking=resolution.enable_thinking,
                    event_granularity=resolution.event_granularity,
                    # ``run_id`` PRÉPARÉ (L1) : le run exposé dans
                    # ``orchestrate.started`` est EXACTEMENT celui exécuté puis
                    # repris — plus de run « fantôme » créé par le worker.
                    run_id=durable_run_id,
                    resume_request_id=resolution.resume_request_id,
                    task_id=resolution.task_id,
                    on_event=relay,
                )
                result_text = json.dumps(
                    multi_agent_result,
                    ensure_ascii=False,
                )
                result = multi_agent_result
                if result.get("awaiting_approval"):
                    approval_payload = dict(result.get("approval") or {}) or None
                    approval_request_id = result.get("request_id")
            else:
                outcome = orchestrate_stream(
                    prompt,
                    session_id=resolution.session_id,
                    scope=resolution.scope,
                    enable_thinking=resolution.enable_thinking,
                    on_event=relay,
                    resume_request_id=resolution.resume_request_id,
                )
                # Tolérance de contrat : ``orchestrate_stream`` renvoie un
                # ``MonoAgentOutcome`` (nouveau) ou un ``AgentRunResult`` brut
                # (compat. historique / tests) — le transport supporte les deux.
                mono_result = getattr(outcome, "result", outcome)
                result = mono_result
                approval_payload = getattr(outcome, "approval", None)
                approval_request_id = (approval_payload or {}).get("request_id")
                result_text = _result_to_text(
                    mono_result,
                    approval=approval_payload,
                    run_id=resolution.run_id,
                    orchestration=resolution.fallback,
                )
            if recorder is not None:
                pending = getattr(result, "awaiting_action", None)
                if pending is not None:
                    recorder.record_approval(
                        tool=str(getattr(pending, "tool", "") or ""),
                        message="Policy : validation humaine requise",
                        request_id=approval_request_id,
                    )
                elif approval_payload is not None:
                    recorder.record_approval(
                        tool=str(approval_payload.get("tool") or ""),
                        message="Policy : validation humaine requise",
                        request_id=approval_request_id,
                    )
                recorder.finish_from_result(result)
            rpc = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": result_text}],
                    "isError": False,
                },
            }
            audit_mcp_call(
                ACT_MCP_ORCHESTRATE,
                subject=client_id,
                detail={
                    "method": "tools/call",
                    "tool": "orchestrate",
                    "is_error": False,
                    "mode": resolution.mode,
                    "requested_mode": resolution.requested_mode,
                    "fallback_reason": (resolution.fallback or {}).get("reason"),
                    "run_id": durable_run_id,
                },
                run_id=str(request_id) if request_id is not None else None,
            )
            emit("orchestrate.done", rpc)
            # Keep the named progress event for existing clients, but also
            # expose the terminal JSON-RPC response as the standard MCP
            # message event. Generic SSE/MCP clients may ignore custom event
            # names and otherwise stop after `orchestrate.started`.
            emit("message", rpc)
        except Exception as exc:
            logger.exception("MCP orchestrate streaming failed")
            if recorder is not None:
                recorder.finish_error(str(exc))
            rpc = {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            }
            audit_mcp_call(
                ACT_MCP_ORCHESTRATE,
                subject=client_id,
                detail={"method": "tools/call", "tool": "orchestrate", "is_error": True},
                run_id=str(request_id) if request_id is not None else None,
            )
            emit("orchestrate.error", rpc)
            emit("message", rpc)
        finally:
            if flow_token is not None:
                clear_call_context(flow_token)
            bridge.close()

    threading.Thread(target=worker, name="mcp-orchestrate-worker", daemon=True).start()
    # Prélude immédiat (fix déployé Render) : sans premier byte rapide, le
    # proxy Render coupe le flux avant que le LLM (lent/injoignable) ne
    # produise son premier event. Le prélude part dès l'ouverture du flux,
    # suivi de heartbeats tant que le worker ne produit rien. L1 (SCRUM-152) :
    # le prélude porte ``run_id`` (reprise/replay) et ``last_sequence``.
    # L1 (SCRUM-152) : l'arrêt client peut survenir DÈS le prélude — ce yield
    # est suspendu HORS de la boucle protégée, un Stop ici ne passerait donc
    # JAMAIS par le ``except`` de la boucle : même contrat d'annulation (sans
    # cela, un Stop juste après ``orchestrate.started`` laissait un run
    # zombie « running » à jamais, lease jamais libéré).
    try:
        yield _sse_event(
            "orchestrate.started",
            {
                "status": "started",
                "mode": resolution.mode,
                "run_id": durable_run_id,
                "resumed": durable_resumed,
                "last_sequence": last_sequence,
            },
        )
    except (asyncio.CancelledError, GeneratorExit):
        disconnected.set()
        _cancel_durable_stream_run(durable_run_id, reason="client disconnected")
        return
    final_emitted = False
    interrupted_reason: str | None = None
    try:
        while True:
            # NOTE : pas de contrôle ``is_disconnected()`` en tête de boucle —
            # un poll à chaque tour coupe le flux dès que le poll est
            # fugacement vrai (proxy/onglet), même en pleine synthèse avec
            # file vide (cas « 44s » : worker.result reçu puis erreur
            # synthétique). La déconnexion n'est constatée qu'après un
            # timeout d'attente (heartbeat), avec drain non-bloquant du
            # terminal éventuellement déjà en file.
            item = await bridge.get(_HEARTBEAT_TIMEOUT_SECONDS)
            if item is _HEARTBEAT:
                if request is not None and await request.is_disconnected():
                    disconnected.set()
                    drained_terminal = False
                    for pending in bridge.drain_nowait():
                        if pending is None:
                            break
                        pending_kind, pending_data = pending
                        if pending_kind in _TERMINAL_SSE_KINDS:
                            final_emitted = True
                            drained_terminal = True
                            yield _sse_event(pending_kind, pending_data)
                            if pending_kind in {
                                "orchestrate.done",
                                "orchestrate.error",
                                "message",
                            }:
                                break
                    logger.info(
                        "Client MCP déconnecté pendant le heartbeat : "
                        "client_id=%s request_id=%s run_id=%s terminal_drainé=%s",
                        client_id,
                        request_id,
                        durable_run_id,
                        drained_terminal,
                    )
                    if not final_emitted:
                        interrupted_reason = "client disconnected"
                    break
                yield ": heartbeat\n\n"
                continue
            if item is None:
                break
            kind, data = item
            if kind in _TERMINAL_SSE_KINDS:
                final_emitted = True
            if not _event_allowed_for_sse(kind, resolution.event_granularity):
                continue
            if kind == "orchestrate.tool":
                yield _sse_event(kind, {"core_tool": data, **data})
            elif kind in {"orchestrate.done", "orchestrate.error"}:
                yield _sse_event(kind, data)
            else:
                yield _sse_event(kind, data)
    except (asyncio.CancelledError, GeneratorExit):
        # Le client a coupé le flux (bouton Stop, onglet fermé) : aucun yield
        # possible ici (GeneratorExit) — le worker daemon termine seul et
        # Starlette ferme la connexion. On signale l'arrêt au worker ET on
        # annule le run durable : sans cela, le run restait ``running`` à
        # jamais (run zombie non repris, lease jamais libéré).
        disconnected.set()
        if not final_emitted:
            _cancel_durable_stream_run(durable_run_id, reason="client disconnected")
        return
    # Clôture normale (worker terminé OU disconnect détecté mais socket encore
    # écrivable) : contrat SSE uniforme trace* + message(JSON-RPC) + [DONE].
    # Sans événement terminal, le front sort de boucle sans finalRpc et lève
    # « sans réponse finale » : on émet une erreur synthétique traçable.
    if not final_emitted:
        synthetic = {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "answer": "",
                                "status": "failed",
                                "failure_phase": "synthesis",
                                "reason": "orchestration_stream_interrupted",
                                "run_id": durable_run_id,
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
                "isError": True,
            },
        }
        logger.warning(
            "Flux MCP terminé sans événement final : erreur synthétique émise "
            "(client_id=%s request_id=%s run_id=%s)",
            client_id,
            request_id,
            durable_run_id,
        )
        # Flux interrompu côté serveur : le run durable ne doit pas rester
        # « running » sans client (reprise possible via un nouveau run).
        _cancel_durable_stream_run(
            durable_run_id,
            reason=interrupted_reason or "orchestration_stream_interrupted",
        )
        yield _sse_event("orchestrate.error", synthetic)
        yield _sse_event("message", synthetic)
    yield "data: [DONE]\n\n"


def _event_allowed_for_sse(kind: str, granularity: str) -> bool:
    """Apply one consistent event policy to both mono and multi-agent runs.

    Les événements terminaux bypassent TOUJOURS le filtre : ils portent la
    réponse finale et ne doivent jamais être abandonnés (invariant 2/3 de
    docs/mcp/MULTI_AGENT_SSE_FLOW.md).
    """
    if kind in _TERMINAL_SSE_KINDS:
        return True
    if granularity == "verbose":
        return True
    if granularity == "minimal":
        return kind in {
            "orchestrate.started",
            "orchestrate.start",
            "orchestrate.done",
            "orchestrate.error",
            "message",
        }
    return (
        kind
        in {
            "orchestrate.started",
            "orchestrate.start",
            "orchestrate.thinking",
            "orchestrate.tool",
            "orchestrate.worker",
            "orchestrate.synthesis",
            "orchestrate.synthesizing",
            "orchestrate.done",
            "orchestrate.error",
            "message",
            "orchestration_fallback",
            # Legacy coordinator event names remain the source of truth for the
            # multi-agent adapter and must not be dropped by the MCP projection.
            "agent.plan",
            "agent.resuming",
            "agent.worker.start",
            "agent.worker.tool",
            "agent.worker.thinking",
            "agent.worker.result",
            "agent.worker.error",
            "agent.worker.approval",
            "agent.phase",
            "agent.synthesizing",
            "agent.done",
            "agent.error",
        }
        or kind.startswith("orchestrate.worker.")
        or kind.startswith("orchestrate.synthesis.")
    )


@router.post("/sse", include_in_schema=False)
async def mcp_sse(
    request: Request,
    mcp_session_id: str | None = Header(default=None, alias="Mcp-Session-Id"),
    x_client_id: str | None = Header(default=None, alias="X-Client-Id"),
) -> Response:
    """Endpoint MCP SSE : JSON-RPC request → flux SSE avec la réponse.

    Le corps de la requête est le message JSON-RPC (texte). La réponse est un
    flux ``text/event-stream`` à événement unique (``message``) — conforme au
    protocole MCP streamable HTTP sans dépendance externe. L'entête
    ``Mcp-Session-Id`` est écho de la session (stateless en S1).

    Tâche 12 (audit) : l'entête optionnelle ``X-Client-Id`` identifie le
    client MCP appelant — chaque appel d'action est journalisé dans
    ``agent_audit`` avec ``subject`` = client_id (repli : id de session,
    sinon ``anonymous``). Auth transport (P5) : ``X-API-Key`` exigée par
    défaut (``MCP_AUTH_REQUIRED=0`` pour un rollback explicite) — vérifiée
    AVANT toute lecture du corps (fail-closed) ; le secret client store
    (révocation par client) reste un durcissement S4+.
    """
    if not mcp_server_enabled():
        return JSONResponse(
            status_code=503,
            content={
                "error": {
                    "code": "mcp_disabled",
                    "message": "MCP server disabled (MCP_SERVER_ENABLED=false)",
                }
            },
        )
    session_id = mcp_session_id or f"tt-{uuid.uuid4().hex[:16]}"
    client_id = (x_client_id or session_id).strip() or "anonymous"
    # Auth transport (P5) : même mécanisme que la surface REST (X-API-Key,
    # repli dev, comparaison à temps constant — module partagé
    # app/infrastructure/security/api_key.py). Vérifié AVANT la lecture du
    # corps : aucune ressource n'est consommée pour une requête non authentifiée.
    if mcp_auth_required() and not is_valid_api_key(request.headers.get("X-API-Key")):
        logger.warning(
            "Requête MCP rejetée (X-API-Key absente/invalide) : client_id=%s session=%s",
            client_id,
            session_id,
        )
        return JSONResponse(
            status_code=401,
            content={
                "error": {
                    "code": "unauthorized",
                    "message": "Invalid or missing X-API-Key header.",
                }
            },
        )
    raw = (await request.body()).decode("utf-8", errors="replace")
    headers = {
        "Mcp-Session-Id": session_id,
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    # Mode stream : `orchestrate` avec `stream`/`enable_thinking` diffuse la
    # réflexion et la progression (`thinking_delta`, `core_tool`) en SSE à
    # événements nommés, puis le JSON-RPC final (`orchestrate.done`).
    try:
        request_payload = json.loads(raw) if raw.strip() else None
    except (ValueError, TypeError):
        request_payload = None
    if _is_streaming_orchestrate(request_payload):
        assert isinstance(request_payload, dict)
        return StreamingResponse(
            _stream_orchestrate(request_payload, client_id=client_id, request=request),
            media_type="text/event-stream",
            headers=headers,
        )
    if _is_durable_replay(request_payload):
        assert isinstance(request_payload, dict)
        return StreamingResponse(
            _replay_durable_events(request_payload),
            media_type="text/event-stream",
            headers=headers,
        )
    # MCP tools may execute synchronous LLM/tool work for several seconds.
    # Keep that work off FastAPI's event loop so independent requests remain
    # responsive while a run is in progress.
    response_payload = await asyncio.to_thread(
        _server.handle_text,
        raw,
        client_id=client_id,
    )
    # Contrat SSE uniforme : même le chemin non-stream termine par [DONE]
    # (les lecteurs stricts front s'arrêtent sur la sentinelle, pas sur la
    # fermeture TCP — sinon « sans réponse finale » sur proxy lent).
    return StreamingResponse(
        iter([_sse_message(response_payload), "data: [DONE]\n\n"]),
        media_type="text/event-stream",
        headers=headers,
    )


__all__ = [
    "configure_mcp_durable_run_store",
    "configure_mcp_orchestration_port",
    "get_mcp_durable_run_store",
    "get_mcp_orchestration_port",
    "mcp_auth_required",
    "mcp_server_enabled",
    "router",
]
