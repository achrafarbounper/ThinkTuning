# project/app/infrastructure/mcp/mcp_flow.py
"""Flow Map MCP — traçage des appels MCP dans le journal « Agent Flow Map ».

Miroir de ``mcp_audit.py`` (S4, tâche 12) : chaque appel MCP d'ACTION est
persisté dans ``app/infrastructure/persistence/flow_store`` (sessions ``agent_flows``) pour
apparaître
dans le dashboard (« Agent Flow Map » — modes Replay / Heatmap) aux côtés des
sessions ``agent.*`` (orchestration multi-agents) et ``core.*`` (noyau v2).

Conventions d'événements (timeline horodatée, ``at_ms`` relatifs au début) :

    - orchestrate — session RICHE (``source="mcp"``) :
        ``mcp.orchestrate.start`` → {role, prompt, client_id, session_id, scope}
        ``mcp.tool``              → {event: tool_start|tool_result, tool, ...}
        ``mcp.thinking``          → {chunk}   (aucune projection graphe)
        ``mcp.approval``          → {role, request_id, message, approval}
        ``mcp.done`` / ``mcp.error`` → terminaison du run
    - actions simples — MINI-session (``source="mcp"``) :
        ``tools/call`` (hors orchestrate) → ``mcp.tool`` (tool_start/result) ;
        ``resources/read`` / ``prompts/get`` / ``sampling/create`` →
        ``mcp.call`` (ouverture) + ``mcp.result`` (clôture).
    - host SORTANT (``CapabilityRouter``) — ``source="mcp_host"`` :
        ``mcp_host.call`` / ``mcp_host.result``.

Exclusions (même policy que l'audit) : ``initialize``, ``ping``,
``tools/list``, ``resources/list``, ``prompts/list`` — catalogue et handshake,
jamais tracés.

Garanties (identiques à l'audit) :
    - NON BLOQUANT : un échec d'écriture ne fait JAMAIS tomber l'appel MCP ;
    - interrupteur ``MCP_FLOW_ENABLED`` (défaut ``true`` — rollback explicite) ;
    - session orchestrate créée UNIQUEMENT si un contexte d'appel est posé par
      le transport (``ContextVar``) — un ``orchestrate()`` appelé hors transport
      (tests unitaires, usage interne) ne produit AUCUNE session.
"""

from __future__ import annotations

import json
import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from app.infrastructure.mcp.protocol import MCPMethod

logger = logging.getLogger("thinktuning.mcp.flow")

# Interrupteur de rollback : lecture à l'import (même convention que
# ``mcp_audit._MCP_AUDIT_ENABLED``) — les tests monkeypatchent l'attribut module.
_MCP_FLOW_ENABLED = os.getenv("MCP_FLOW_ENABLED", "").strip().lower() not in {
    "false",
    "0",
    "no",
    "off",
}

# Host sortant : session dédiée par appel distant quand aucune session
# orchestrate n'est ouverte (``MCP_FLOW_HOST_STANDALONE=false`` → silencieux).
_MCP_HOST_STANDALONE = os.getenv("MCP_FLOW_HOST_STANDALONE", "").strip().lower() not in {
    "false",
    "0",
    "no",
    "off",
}

# --- Noms d'événements persistés (contrat frontend ``flowmap/events.ts``) ----

MCP_ORCHESTRATE_START = "mcp.orchestrate.start"
MCP_TOOL = "mcp.tool"
MCP_THINKING = "mcp.thinking"
MCP_APPROVAL = "mcp.approval"
MCP_DONE = "mcp.done"
MCP_ERROR = "mcp.error"
MCP_CALL = "mcp.call"
MCP_RESULT = "mcp.result"
MCP_HOST_CALL = "mcp_host.call"
MCP_HOST_RESULT = "mcp_host.result"

# Statut du run (RunStatus / libellé API) → statut de session Flow Map
# (``app/infrastructure/persistence/flow_store.STATUSES`` — aligné sur
# ``run_lifecycle.RUN_STATUS_TO_API``).
_RUN_TO_FLOW: dict[str, str] = {
    "completed": "completed",
    "pending_approval": "awaiting_approval",
    "awaiting_approval": "awaiting_approval",  # libellé API (défensif)
    "rejected": "rejected",
    "rejected_loop": "rejected",
    "error": "error",
    "failed": "error",
    "budget_exhausted": "error",
}

# Libellés d'action pour les méthodes hors ``tools/call``.
_ACTION_EVENT_BY_METHOD = {
    MCPMethod.RESOURCES_READ: "resources/read",
    MCPMethod.PROMPTS_GET: "prompts/get",
    MCPMethod.SAMPLING_CREATE: "sampling/create",
}


def mcp_flow_enabled() -> bool:
    """Le traçage Flow Map MCP est-il activé ? (interrupteur de rollback)."""
    return _MCP_FLOW_ENABLED


# --- Contexte d'appel (posé par les transports, lu par ``orchestrate``) ------


@dataclass(frozen=True)
class MCPCallContext:
    """Contexte transport d'un appel MCP entrant (``ContextVar``, par thread).

    Posé par ``MCPServer._handle_method`` (transport stdio / SSE non-streaming)
    et par le worker du transport SSE streaming — consommé par
    ``begin_orchestrate_flow`` pour la session riche du run.
    """

    client_id: str = "anonymous"
    request_id: str | None = None
    session_id: str | None = None


_CALL_CONTEXT: ContextVar[MCPCallContext | None] = ContextVar("mcp_call_context", default=None)
_CURRENT_RECORDER: ContextVar[MCPFlowRecorder | None] = ContextVar(
    "mcp_flow_recorder", default=None
)


def current_call_context() -> MCPCallContext | None:
    """Contexte d'appel courant (``None`` hors transport — tests unitaires)."""
    return _CALL_CONTEXT.get()


def set_call_context(ctx: MCPCallContext) -> object:
    """Pose le contexte d'appel (retourne le token pour ``clear_call_context``)."""
    return _CALL_CONTEXT.set(ctx)


def clear_call_context(token: object) -> None:
    """Retire le contexte d'appel posé par ``set_call_context``."""
    try:
        _CALL_CONTEXT.reset(token)  # type: ignore[arg-type]
    except ValueError:  # pragma: no cover - thread différent (défensif)
        _CALL_CONTEXT.set(None)


def current_recorder() -> MCPFlowRecorder | None:
    """Recorder de la session orchestrate ouverte sur ce thread (host sortant)."""
    return _CURRENT_RECORDER.get()


# --- Recorder -----------------------------------------------------------------


class MCPFlowRecorder:
    """Recorder d'une session Flow Map MCP — écritures JAMAIS bloquantes.

    Chaque événement est horodaté en millisecondes relatives au début de la
    session (même timeline que les sessions ``agent.*`` / ``core.*``). Toute
    erreur de persistance est avalée (log) : le traçage ne doit jamais altérer
    l'appel MCP tracé.
    """

    def __init__(
        self,
        flow_id: str,
        *,
        client_id: str = "",
        request_id: str | None = None,
        event_granularity: str = "summary",
        parent_task_id: str | None = None,
        worker_id: str | None = None,
    ) -> None:
        self.flow_id = flow_id
        self.client_id = client_id
        self.request_id = request_id
        self.event_granularity = str(event_granularity or "summary").strip().lower()
        if self.event_granularity not in {"minimal", "summary", "verbose"}:
            self.event_granularity = "summary"
        self.parent_task_id = parent_task_id
        self.worker_id = worker_id
        self._t0 = time.perf_counter()
        self._finished = False
        # ``True`` pour les sessions host sortantes isolées (clôture à la fin
        # de l'appel) ; ``False`` pour la session orchestrate du thread courant.
        self._standalone = False

    def _normalize_event_data(self, event: str, data: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(data or {})
        normalized.setdefault("parent_task_id", self.parent_task_id)
        normalized.setdefault("worker_id", self.worker_id)
        if "phase" not in normalized:
            if event.startswith("mcp.orchestrate.worker"):
                normalized["phase"] = "worker"
            elif event.startswith("mcp.orchestrate.synthesis"):
                normalized["phase"] = "synthesis"
            else:
                normalized["phase"] = "lead"
        if self.event_granularity == "minimal" and event not in {
            MCP_ORCHESTRATE_START,
            MCP_DONE,
            MCP_ERROR,
        }:
            return {}
        return normalized

    # --- Écriture -------------------------------------------------------------

    def record(self, event: str, data: dict[str, Any]) -> None:
        """Persiste un événement (non bloquant — erreurs avalées)."""
        if self._finished:
            return
        payload = self._normalize_event_data(event, data)
        if not payload:
            return
        try:
            from app.infrastructure.persistence.flow_store import get_flow_store

            at_ms = (time.perf_counter() - self._t0) * 1000.0
            get_flow_store().append_event(self.flow_id, event, payload, at_ms)
        except Exception:  # pragma: no cover - le flow ne doit JAMAIS remonter
            logger.exception("Flow MCP : écriture impossible (%s)", event)

    def record_tool(self, event: dict[str, Any]) -> None:
        """Trace un événement d'outil du noyau (``tool_start`` / ``tool_result``)."""
        data = dict(event or {})
        data.setdefault("role", "Agent MCP")
        self.record(MCP_TOOL, data)

    def record_thinking(self, chunk: str) -> None:
        """Trace un delta de réflexion (timeline seule — non graphé)."""
        self.record(MCP_THINKING, {"chunk": chunk, "role": "Agent MCP"})

    def record_approval(
        self,
        *,
        tool: str,
        message: str = "",
        request_id: str | None = None,
    ) -> None:
        """Trace une action soumise à validation humaine (``pending_approval``)."""
        self.record(
            MCP_APPROVAL,
            {
                "role": "Agent MCP",
                "request_id": request_id,
                "message": message or "Policy : validation humaine requise",
                "approval": {"tool": tool},
            },
        )

    # [CLASSE-SUITE]

    # --- Clôture ----------------------------------------------------------------

    def finish(
        self,
        status: str,
        answer_summary: str = "",
        error: str | None = None,
    ) -> None:
        """Clôture la session Flow Map (idempotent, non bloquant)."""
        if self._finished:
            return
        self._finished = True
        try:
            if _CURRENT_RECORDER.get() is self:
                _CURRENT_RECORDER.set(None)
        except Exception:  # pragma: no cover - défensif
            pass
        try:
            from app.infrastructure.persistence.flow_store import get_flow_store

            get_flow_store().finish_flow(
                self.flow_id,
                status,
                answer_summary=answer_summary or "",
                error=error,
            )
        except Exception:  # pragma: no cover
            logger.exception("Flow MCP : clôture impossible (%s)", self.flow_id)

    def finish_error(self, message: str) -> None:
        """Clôture en ``error`` avec un événement ``mcp.error``."""
        self.record(MCP_ERROR, {"message": message, "role": "Agent MCP"})
        self.finish("error", error=message)

    def finish_from_result(self, result: Any) -> None:
        """Clôture depuis un résultat mono- ou multi-agent."""
        if isinstance(result, dict):
            raw_status = result.get("status", "")
            answer = str(result.get("answer", "") or "")
        else:
            raw_status = getattr(result, "status", "")
            answer = str(getattr(result, "answer", "") or "")
        run_status = str(getattr(raw_status, "value", raw_status) or "")
        flow_status = _RUN_TO_FLOW.get(run_status, "error")
        self.record(MCP_DONE, {"answer": answer, "status": flow_status, "role": "Agent MCP"})
        self.finish(flow_status, answer_summary=answer[:300])


# [ORCH-SUITE]

# --- Ouverture / clôture de la session orchestrate -----------------------------


def begin_orchestrate_flow(
    *,
    prompt: str,
    session_id: str = "default",
    scope: str = "default",
) -> MCPFlowRecorder | None:
    """Ouvre la session RICHE d'un run orchestrate (``tools/call orchestrate``).

    Session créée UNIQUEMENT si un contexte d'appel est posé par le transport
    (un ``orchestrate()`` appelé hors transport — tests unitaires, usage interne
    — ne produit AUCUNE session). Non bloquant : ``None`` en cas d'échec.
    """
    if not _MCP_FLOW_ENABLED:
        return None
    ctx = _CALL_CONTEXT.get()
    if ctx is None:
        return None
    try:
        from app.infrastructure.persistence.flow_store import get_flow_store

        row = get_flow_store().start_flow(prompt, model=_resolve_model(), source="mcp")
        recorder = MCPFlowRecorder(
            str(row.get("id") or ""), client_id=ctx.client_id, request_id=ctx.request_id
        )
        recorder.record(
            MCP_ORCHESTRATE_START,
            {
                "role": "Agent MCP",
                "prompt": prompt,
                "client_id": ctx.client_id,
                "request_id": ctx.request_id,
                "session_id": session_id,
                "scope": scope,
            },
        )
        _CURRENT_RECORDER.set(recorder)
        return recorder
    except Exception:  # pragma: no cover - le flow ne doit JAMAIS remonter
        logger.exception("Flow MCP : ouverture orchestrate impossible")
        return None


def _resolve_model() -> str:
    """Modèle LLM de l'agent (réglage IHM) — jamais bloquant, vide en repli."""
    try:
        from app.agent.settings import get_agent_config

        cfg = get_agent_config()
        try:
            return str(cfg["model"] or "")  # type: ignore[index]
        except (TypeError, KeyError, IndexError):
            return str(getattr(cfg, "model", "") or "")
    except Exception:  # pragma: no cover - réglage indisponible
        return ""


# [ACTION-SUITE]

# --- MINI-sessions des actions simples (côté serveur MCPServer) ----------------


def trace_action_flow(
    method: str,
    params: dict[str, Any],
    response: Any,
    client_id: str = "anonymous",
    request_id: Any = None,
) -> None:
    """Persiste une MINI-session pour un appel d'action MCP simple.

    ``tools/call`` (hors orchestrate) → événements ``mcp.tool`` (tool_start +
    tool_result) ; ``resources/read`` / ``prompts/get`` / ``sampling/create`` →
    ``mcp.call`` + ``mcp.result``. Non bloquant : toute erreur est avalée.
    """
    if not _MCP_FLOW_ENABLED:
        return
    try:
        from app.infrastructure.persistence.flow_store import COMPLETED as FLOW_COMPLETED
        from app.infrastructure.persistence.flow_store import ERROR as FLOW_ERROR
        from app.infrastructure.persistence.flow_store import get_flow_store

        method = str(method or "")
        is_error = _is_error_response(response)
        summary = summarize_mcp_result(response)
        label = _action_label(method, params)
        if method == MCPMethod.TOOLS_CALL:
            tool = str(label or "?")
            prompt = f"[MCP] {tool}"
            events: list[tuple[str, dict[str, Any]]] = [
                (
                    MCP_TOOL,
                    {
                        "event": "tool_start",
                        "tool": tool,
                        "args": _arguments(params),
                        "role": "MCP",
                        "client_id": client_id,
                    },
                ),
                (
                    MCP_TOOL,
                    {
                        "event": "tool_result",
                        "tool": tool,
                        "role": "MCP",
                        "status": "error" if is_error else "ok",
                        "summary": summary,
                    },
                ),
            ]
        else:
            kind = _ACTION_EVENT_BY_METHOD.get(method, method)
            prompt = f"[MCP] {kind}" + (f" {label}" if label else "")
            events = [
                (
                    MCP_CALL,
                    {"method": kind, "label": label, "role": "MCP", "client_id": client_id},
                ),
                (
                    MCP_RESULT,
                    {
                        "method": kind,
                        "label": label,
                        "role": "MCP",
                        "status": "error" if is_error else "ok",
                        "answer": summary,
                    },
                ),
            ]
        row = get_flow_store().start_flow(prompt, model="mcp", source="mcp")
        recorder = MCPFlowRecorder(
            str(row.get("id") or ""), client_id=client_id, request_id=_rid(request_id)
        )
        for event, data in events:
            recorder.record(event, data)
        recorder.finish(
            FLOW_ERROR if is_error else FLOW_COMPLETED,
            answer_summary=summary[:300],
        )
    except Exception:  # pragma: no cover - le flow ne doit JAMAIS remonter
        logger.exception("Flow MCP : traçage impossible (%s)", method)


# [HELPERS-SUITE]


def _action_label(method: str, params: dict[str, Any]) -> str:
    """Libellé lisible de l'action (tool / URI / prompt / modèle)."""
    try:
        if not isinstance(params, dict):
            return ""
        if method == MCPMethod.TOOLS_CALL:
            return str(params.get("name") or "")
        if method == MCPMethod.RESOURCES_READ:
            return str(params.get("uri") or "")
        if method == MCPMethod.PROMPTS_GET:
            return str(params.get("name") or "")
        if method == MCPMethod.SAMPLING_CREATE:
            return str(params.get("model") or "")
    except Exception:  # pragma: no cover
        return ""
    return ""


def _arguments(params: dict[str, Any]) -> dict[str, Any]:
    """Arguments du tool (``params.arguments``) — vide si absents/malformés."""
    args = params.get("arguments") if isinstance(params, dict) else None
    return dict(args) if isinstance(args, dict) else {}


def _rid(request_id: Any) -> str | None:
    """``request_id`` JSON-RPC normalisé en str (``None`` → ``None``)."""
    return None if request_id is None else str(request_id)


def _is_error_response(payload: Any) -> bool:
    """La réponse JSON-RPC est-elle une erreur (enveloppe ou ``isError``) ?"""
    try:
        if not isinstance(payload, dict):
            return False
        if "error" in payload:
            return True
        result = payload.get("result")
        return isinstance(result, dict) and result.get("isError") is True
    except Exception:  # pragma: no cover
        return False


def summarize_mcp_result(payload: Any) -> str:
    """Résumé lisible d'une réponse MCP (texte du 1er bloc, ou message d'erreur)."""
    try:
        if not isinstance(payload, dict):
            return ""
        err = payload.get("error")
        if isinstance(err, dict):
            code = err.get("code", "")
            message = err.get("message", "")
            return f"{code} {message}".strip()
        result = payload.get("result")
        if isinstance(result, dict):
            for item in result.get("content") or []:
                if isinstance(item, dict) and item.get("text"):
                    return str(item["text"])[:200]
    except Exception:  # pragma: no cover
        return ""
    return ""


def _first_text_block(payload: Any) -> str:
    """Premier bloc texte COMPLET d'une réponse ``CallToolResult`` ('' sinon).

    Non tronqué (contrairement à ``summarize_mcp_result``) : la clôture d'une
    session orchestrate doit pouvoir relire le JSON sérialisé du tool.
    """
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result")
    if isinstance(result, dict):
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("text"):
                return str(item["text"])
    return ""


def close_orchestrate_flow(recorder: MCPFlowRecorder | None, response: Any) -> None:
    """Clôture une session orchestrate depuis la réponse JSON-RPC du tool.

    Appelé par ``MCPServer._handle_method`` APRÈS le dispatch de
    ``tools/call orchestrate`` (session riche ouverte avant le run). Non
    bloquant et idempotent (``MCPFlowRecorder.finish`` l'est déjà) :
        - échec (enveloppe JSON-RPC ``error`` ou ``isError``) → ``mcp.error``
          + statut ``error`` ;
        - succès → ``mcp.done`` + statut déduit du payload JSON sérialisé par
          le tool orchestrate (``{answer, status, awaiting_approval, ...}``),
          mappé sur les statuts Flow Map via ``_RUN_TO_FLOW``.
    """
    if recorder is None:
        return
    try:
        if _is_error_response(response):
            recorder.finish_error(summarize_mcp_result(response) or "orchestrate a échoué")
            return
        text = _first_text_block(response)
        status = "completed"
        answer = ""
        if text:
            try:
                payload = json.loads(text)
                if isinstance(payload, dict):
                    raw_status = str(payload.get("status") or "")
                    status = _RUN_TO_FLOW.get(raw_status, "completed")
                    answer = str(payload.get("answer") or "")
            except (ValueError, TypeError):  # texte non-JSON → repli completed
                pass
        recorder.record(MCP_DONE, {"answer": answer, "status": status, "role": "Agent MCP"})
        recorder.finish(status, answer_summary=(answer or "")[:300])
    except Exception:  # pragma: no cover - le flow ne doit JAMAIS remonter
        logger.exception("Flow MCP : clôture orchestrate impossible — non bloquant")


# [HOST-SUITE]

# --- Host SORTANT (CapabilityRouter → serveur MCP distant) ---------------------


def host_call_started(
    *,
    tool: str,
    server: str = "",
    remote: str = "",
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Ouvre le traçage d'un appel MCP SORTANT (host → serveur distant).

    Deux modes :
        - session orchestrate ouverte sur ce thread → événement
          ``mcp_host.call`` dans la session COURANTE (le host est un outil de
          l'agent en train de s'exécuter) ;
        - aucune session → session dédiée ``source="mcp_host"``
          (``MCP_FLOW_HOST_STANDALONE=false`` → silencieux).

    Retourne un token à passer à ``host_call_finished`` (``None`` → no-op).
    """
    if not _MCP_FLOW_ENABLED:
        return None
    try:
        data: dict[str, Any] = {
            "tool": tool,
            "server": server,
            "remote": remote,
            "role": "MCP Host",
            "arguments": dict(arguments or {}),
        }
        recorder = _CURRENT_RECORDER.get()
        if recorder is not None:
            recorder.record(MCP_HOST_CALL, data)
            return {"recorder": recorder, "data": data, "standalone": False}
        if not _MCP_HOST_STANDALONE:
            return None
        from app.infrastructure.persistence.flow_store import get_flow_store

        row = get_flow_store().start_flow(
            f"[MCP host] {server or '?'} · {remote or tool}",
            model="mcp_host",
            source="mcp_host",
        )
        recorder = MCPFlowRecorder(str(row.get("id") or ""), client_id=tool)
        recorder._standalone = True
        recorder.record(MCP_HOST_CALL, data)
        return {"recorder": recorder, "data": data, "standalone": True}
    except Exception:  # pragma: no cover - le flow ne doit JAMAIS remonter
        return None


# [HOST-FIN]


def host_call_finished(
    token: dict[str, Any] | None,
    *,
    is_error: bool = False,
    summary: str = "",
) -> None:
    """Clôture le traçage d'un appel sortant (no-op si ``token`` est ``None``)."""
    if not token:
        return
    try:
        recorder: MCPFlowRecorder | None = token.get("recorder")
        if recorder is None or recorder._finished:
            return
        data = dict(token.get("data") or {})
        data.pop("arguments", None)
        data["status"] = "error" if is_error else "ok"
        data["summary"] = summary or ""
        recorder.record(MCP_HOST_RESULT, data)
        if token.get("standalone"):
            from app.infrastructure.persistence.flow_store import COMPLETED as FLOW_COMPLETED
            from app.infrastructure.persistence.flow_store import ERROR as FLOW_ERROR

            recorder.finish(
                FLOW_ERROR if is_error else FLOW_COMPLETED,
                answer_summary=(summary or "")[:300],
            )
    except Exception:  # pragma: no cover - défensif
        pass


__all__ = [
    "MCP_APPROVAL",
    "MCP_CALL",
    "MCP_DONE",
    "MCP_ERROR",
    "MCP_HOST_CALL",
    "MCP_HOST_RESULT",
    "MCP_ORCHESTRATE_START",
    "MCP_RESULT",
    "MCP_THINKING",
    "MCP_TOOL",
    "MCPCallContext",
    "MCPFlowRecorder",
    "begin_orchestrate_flow",
    "clear_call_context",
    "close_orchestrate_flow",
    "current_call_context",
    "current_recorder",
    "host_call_finished",
    "host_call_started",
    "mcp_flow_enabled",
    "set_call_context",
    "summarize_mcp_result",
    "trace_action_flow",
]
