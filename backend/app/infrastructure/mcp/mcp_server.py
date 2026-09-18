# project/app/infrastructure/mcp/mcp_server.py
"""Cœur serveur MCP — dispatch JSON-RPC 2.0 partagé par les transports SSE et stdio.

Séparation des responsabilités (hexagonale, docs/mcp/MCP_IMPLEMENTATION_MAPPING.md) :
    - ce module connaît la SÉMANTIQUE MCP (initialize, ping, tools/list,
      tools/call…) et le scope de sécurité — mais AUCUN transport ;
    - ``mcp_server_sse.py`` / ``mcp_server_stdio.py`` ne font que câbler ce
      cœur sur leur entrée/sortie ;
        - la source de vérité des tools est formalisée en port domaine
      ``MCPToolRegistryPort`` (app/domain/ports/mcp_ports.py, tâche 3) :
      ``ToolProvider`` est un alias rétrocompatible. La fabrique
      (``mcp_server_factory.py``) fournit le registre bootstrap de la S1.

S1 = dispatch SYNCHRONE : tous les handlers de tools du bootstrap sont purs.
L'asynchronicité des futurs tools (sampling / orchestrate, S6) s'ajoutera par
évolution des handlers, sans changer le contrat ``handle_text``.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from typing import Any

from app.domain.entities.mcp import (
    MCPScopeRole,
    MCPTool,
    MCPVersion,
    SamplingRequest,
)
from app.domain.errors import LLMClientError, NotFoundError, ValidationError
from app.domain.ports.mcp_ports import (
    MCPPromptRegistryPort,
    MCPResourceRegistryPort,
    MCPToolRegistryPort,
    SamplingPort,
)
from app.infrastructure.mcp.catalog_pagination import (
    KIND_PROMPTS,
    KIND_RESOURCES,
    KIND_TOOLS,
    CursorError,
    decode_cursor,
    default_page_size,
    encode_cursor,
    paginate,
)
from app.infrastructure.mcp.error_contract import (
    fallback_from_rpc_code,
    new_correlation_id,
    sanitize_message,
    structured_internal,
    structured_not_found,
    structured_timeout,
    structured_validation,
)
from app.infrastructure.mcp.mcp_events import build_meta
from app.infrastructure.mcp.mcp_flow import (
    MCPCallContext,
    MCPFlowRecorder,
    begin_orchestrate_flow,
    clear_call_context,
    close_orchestrate_flow,
    set_call_context,
    trace_action_flow,
)
from app.infrastructure.mcp.mcp_metrics import (
    record_request_latency,
    record_tool_call,
)
from app.infrastructure.mcp.protocol import (
    MCP_PROTOCOL_VERSION,
    MCP_SERVER_NAME,
    ErrorCode,
    MCPMethod,
    ProtocolError,
    error_result,
    parse_jsonrpc,
    success_result,
)

# S4, tâche 12 : actions d'audit normalisées MCP
from app.infrastructure.persistence.audit_store import (
    ACT_MCP_ORCHESTRATE,
    ACT_MCP_PROMPT_GET,
    ACT_MCP_RESOURCE_READ,
    ACT_MCP_SAMPLING,
    ACT_MCP_TOOL_CALL,
    ACT_MCP_TOOL_DEPRECATED,
)

logger = logging.getLogger("thinktuning.mcp.server")

# Actions d'audit par méthode MCP (tâche 12). ``tools/call`` est traité à part :
# le nom de tool tranche entre ``ACT_MCP_TOOL_CALL`` et ``ACT_MCP_ORCHESTRATE``.
_AUDIT_ACTION_BY_METHOD: dict[str, str | None] = {
    MCPMethod.TOOLS_CALL: None,  # résolu par nom de tool (orchestrate vs restant)
    MCPMethod.RESOURCES_READ: ACT_MCP_RESOURCE_READ,
    MCPMethod.PROMPTS_GET: ACT_MCP_PROMPT_GET,
    MCPMethod.SAMPLING_CREATE: ACT_MCP_SAMPLING,
}

# Méthodes d'ACTION traçées dans la Flow Map (même policy que l'audit :
# catalogue/handshake — initialize, ping, tools/list… — jamais tracés).
# ``tools/call`` déclenche une session RICHE (orchestrate) ou une MINI-session
# (les autres tools) via ``mcp_flow`` ; ``resources/read``, ``prompts/get`` et
# ``sampling/create`` produisent ``mcp.call`` + ``mcp.result``.
_FLOW_METHODS = frozenset(
    {
        MCPMethod.TOOLS_CALL,
        MCPMethod.RESOURCES_READ,
        MCPMethod.PROMPTS_GET,
        MCPMethod.SAMPLING_CREATE,
    }
)


def _request_run_id(request_id: Any) -> str | None:
    """``run_id`` MCP : id JSON-RPC normalisé en str (``mcp_request_id``)."""
    if request_id is None:
        return None
    return str(request_id)


def resolve_correlation_id(payload: dict[str, Any] | None, *, provided: str | None = None) -> str:
    """Résout le ``correlation_id`` d'une requête MCP (MCP 2.3.0 — observabilité).

    Priorité : ``params._meta.correlationId`` (le client fournit SON
    identifiant — chaîne de corrélation de bout en bout) > ``provided``
    (en-tête transport ``X-Correlation-Id``) > généré localement
    (``new_correlation_id`` — 12 hex, identique au contrat d'erreurs).

    Un identifiant fourni est SANITISÉ : borné à 64 caractères alphanumériques
    (``-``, ``_``, ``.``) — jamais de valeur arbitraire dans les logs/audit.

    Publique (préfixe sans ``_``) : les TRANSPORTS l'appellent AVANT de
    déléguer à ``MCPServer.handle_text`` pour échoir le MÊME identifiant dans
    la réponse HTTP (``X-Correlation-Id``) et dans ses propres logs. La
    résolution étant déterministe, transport et serveur convergent sur la
    même valeur pour un même message.

    Args:
        payload: message JSON-RPC parsé (``None`` pour un corps illisible) ;
        provided: identifiant porté par le transport (en-tête), prioritaire
            seulement en l'absence de ``params._meta.correlationId``.

    Returns:
        Identifiant de corrélation non vide (jamais ``None``).
    """
    params = payload.get("params") if isinstance(payload, dict) else None
    meta_cid = ""
    if isinstance(params, dict):
        meta = params.get("_meta")
        if isinstance(meta, dict):
            raw = str(meta.get("correlationId") or "").strip()
            if raw:
                meta_cid = raw
    candidate = meta_cid or str(provided or "").strip()
    if not candidate:
        return new_correlation_id()
    # Sanitisation : 64 caractères max, alphanumériques + ``-_.`` — sinon régénéré.
    candidate = re.sub(r"[^A-Za-z0-9._-]", "", candidate)[:64]
    return candidate or new_correlation_id()


class ToolError(RuntimeError):
    """Erreur métier d'un tool → réponse MCP ``isError: true``.

    Distinguée des erreurs de PROTOCOLE : un tool peut échouer (API externe
    injoignable, argument refusé…) sans que le transport JSON-RPC soit fautif.
    """


# MCPTool et ToolProvider sont désormais dans le domaine (tâche 3) :
#   - MCPTool ← app/domain/entities/mcp.py (entité pure, déplacée de l'infra)
#   - MCPToolRegistryPort ← app/domain/ports/mcp_ports.py (Protocol)
# ToolProvider est conservé comme alias rétrocompatible pour les imports existants.
ToolProvider = MCPToolRegistryPort


class MCPServer:
    """Dispatch JSON-RPC 2.0 / MCP, stateless et thread-safe.

        Instance par client ou partagée : aucun état mutable entre requêtes (les
    handlers de tools passent par le ``MCPToolRegistryPort`` injecté). Le scope
    de sécurité est FIXÉ à la construction (fabrique, build with scope).

    Attributes:
        name:           nom du serveur (``serverInfo.name``) ;
        version:        version de la surface MCP (``serverInfo.version``) ;
        scope:          rôle du client (filtre la visibilité des tools) ;
        tool_provider:  source des tools (port ``MCPToolRegistryPort``, alias
            rétrocompatible ``ToolProvider``) ;
        resource_provider: source des resources (port ``MCPResourceRegistryPort``,
            tâche 8 : 5 resources ``thinktuning://``) — ``None`` → surface
            sans resources (comportement v0.1.0 des constructions sur mesure) ;
                prompt_provider: source des prompts (port ``MCPPromptRegistryPort``,
            tâche 9 : 2 prompts ThinkTuning) — ``None`` → surface sans
            prompts (comportement v0.1.0 des constructions sur mesure) ;
        sampling_port: port ``SamplingPort`` (tâche 15, S6 v2.0.0) — reverse
            LLM inference (``sampling/create``) — ``None`` → surface sans
            capacité sampling (comportement < v2.0.0, fail-closed) ;
        audit: hook d'audit ``(action, *, subject, detail, run_id)`` invoqué
            pour chaque appel MCP d'action (tâche 12) — ``None`` → aucune
            écriture (les transports branchent ``mcp_audit.audit_mcp_call``).
        page_size: taille MAX de page des catalogues (v2.3.0 — pagination par
            curseur opaque sur ``tools/list`` / ``resources/list`` /
            ``prompts/list``) ; ``None`` → env ``MCP_PAGINATION_PAGE_SIZE``
            (50). Sans ``params.cursor``, la réponse reste identique aux 2.2.x.
    """

    def __init__(
        self,
        *,
        name: str = MCP_SERVER_NAME,
        version: MCPVersion,
        scope: MCPScopeRole,
        tool_provider: MCPToolRegistryPort,
        resource_provider: MCPResourceRegistryPort | None = None,
        prompt_provider: MCPPromptRegistryPort | None = None,
        sampling_port: SamplingPort | None = None,
        audit: Callable[..., Any] | None = None,
        page_size: int | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.scope = scope
        self.tool_provider = tool_provider
        self.resource_provider = resource_provider
        self.prompt_provider = prompt_provider
        # Port de sampling (S6, v2.0.0) : ``None`` → sampling/create rejeté
        # (comportement rétrocompatible < v2.0.0). Le transport SSE branche
        # ``build_sampling_adapter()`` via la fabrique.
        self.sampling_port = sampling_port
        # Hook d'audit injecté (S4, tâche 12) : ``None`` → aucune écriture (les
        # transports SSE/stdio branchent ``app.infrastructure.mcp.mcp_audit``).
        self.audit = audit
        # Pagination des catalogues (v2.3.0) : taille MAX de page configurable
        # (``MCPServer(page_size=...)`` > env ``MCP_PAGINATION_PAGE_SIZE`` > 50).
        # Défaut 50 > tailles des catalogues actuels : sans ``params.cursor``,
        # la réponse reste IDENTIQUE aux versions 2.2.x (compatibilité).
        self.page_size = max(1, int(page_size)) if page_size is not None else default_page_size()

    # --- Surface publique --------------------------------------------------------

    def handle_text(
        self,
        raw: str,
        *,
        client_id: str = "anonymous",
        correlation_id: str | None = None,
    ) -> str | None:
        """Parse un message JSON-RPC (texte brut) et retourne la réponse encodée.

        Args :
            raw : corps JSON-RPC (texte) ;
            client_id : identité du client MCP appelant — portée en
                ``subject`` de chaque entrée d'audit produite par cet appel
                (le transport la résout depuis son en-tête / sa session) ;
            correlation_id : identifiant de corrélation porté par le TRANSPORT
                (en-tête ``X-Correlation-Id``). Priorité au champ
                ``params._meta.correlationId`` fourni par le client ; en
                l'absence des deux, un identifiant est généré (MCP 2.3.0 —
                observabilité : logs + audit + réponse partagent le même id).

        Returns:
            La réponse JSON-RPC sérialisée à émettre, ou ``None`` pour une
            notification (MCP : aucune réponse attendue sur le transport).
        """
        try:
            payload = parse_jsonrpc(raw)
        except ProtocolError as exc:
            return self._encode(
                error_result(
                    None,
                    exc.code,
                    exc.message,
                    data=fallback_from_rpc_code(exc.code, exc.message).to_data(),
                )
            )
        # MCP 2.3.0 — contrat d'erreurs structuré : un correlation_id par
        # requête, injecté dans TOUTE erreur émise pour cette requête (et
        # journalisé) — un seul identifiant relie logs + audit + réponse.
        # MCP 2.3.0 — observabilité : le client peut FOURNIR son identifiant
        # (``params._meta.correlationId``) ou le transport le sien
        # (``X-Correlation-Id``, paramètre ``correlation_id``) — corrélation de
        # bout en bout logs ↔ audit ↔ réponse.
        correlation_id = resolve_correlation_id(
            payload if isinstance(payload, dict) else None,
            provided=correlation_id,
        )
        latency_start = time.perf_counter()
        try:
            response = self._dispatch(
                payload, client_id=client_id, correlation_id=correlation_id
            )
        except ProtocolError as exc:
            contract = fallback_from_rpc_code(exc.code, exc.message, correlation_id=correlation_id)
            logger.warning(
                "MCP erreur protocole correlation_id=%s method=%s code=%s : %s",
                correlation_id,
                payload.get("method") if isinstance(payload, dict) else "<batch>",
                exc.code,
                exc.message,
            )
            response = error_result(None, exc.code, exc.message, data=contract.to_data())
        else:
            error = response.get("error") if isinstance(response, dict) else None
            if isinstance(error, dict) and "data" not in error:
                # Filet central : toute erreur non taguée par un handler
                # reçoit un contrat minimal dérivé du code JSON-RPC.
                error["data"] = fallback_from_rpc_code(
                    int(error.get("code", ErrorCode.INTERNAL_ERROR)),
                    str(error.get("message", "")),
                    correlation_id=correlation_id,
                ).to_data()
        # Observabilité MCP 2.3.0 : latence + volume par méthode (défensif —
        # les métriques ne doivent JAMAIS altérer la réponse).
        try:
            method_label = (
                payload.get("method")
                if isinstance(payload, dict) and isinstance(payload.get("method"), str)
                else "unknown"
            )
            record_request_latency(method_label, time.perf_counter() - latency_start)
            if method_label == MCPMethod.TOOLS_CALL:
                params_mc = payload.get("params") if isinstance(payload, dict) else None
                tool_name = (
                    str(params_mc.get("name") or "") if isinstance(params_mc, dict) else ""
                )
                record_tool_call(
                    tool_name,
                    is_error=self._response_is_error(response)
                    if isinstance(response, dict)
                    else False,
                )
        except Exception:  # pragma: no cover - observabilité jamais bloquante
            logger.debug("MCP métriques indisponibles", exc_info=True)
        logger.debug("MCP requête correlation_id=%s client=%s", correlation_id, client_id)
        return self._encode(response)

    # --- Dispatch -------------------------------------------------------------------

    def _dispatch(
        self,
        payload: dict[str, Any] | list[Any],
        *,
        client_id: str,
        correlation_id: str = "",
    ) -> dict[str, Any] | None:
        """Dispatch d'un message déjà parsé → enveloppe JSON-RPC (ou None)."""
        if not isinstance(payload, dict):
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST,
                "Invalid Request: batch requests are not supported",
            )
        method = payload.get("method")
        is_notification = "id" not in payload
        if not isinstance(method, str):
            if is_notification:
                return None
            raise ProtocolError(
                ErrorCode.INVALID_REQUEST, "Invalid Request: 'method' must be a string"
            )
        if is_notification:
            self._handle_notification(method, correlation_id=correlation_id)
            return None
        params = payload.get("params", {})
        request_id = payload.get("id")
        if not isinstance(params, dict):
            raise ProtocolError(
                ErrorCode.INVALID_PARAMS,
                "Invalid params: 'params' must be an object",
            )
        return self._handle_method(
            method, request_id, params, client_id, correlation_id=correlation_id
        )

    def _handle_method(
        self,
        method: str,
        request_id: Any,
        params: dict[str, Any],
        client_id: str,
        *,
        correlation_id: str = "",
    ) -> dict[str, Any]:
        """Dispatch d'une méthode de REQUÊTE (id présent) → réponse JSON-RPC.

        Chaque méthode d'ACTION (tools/call, resources/read, prompts/get,
        sampling/create) est ensuite AUDITÉE de façon centralisée
        (``_audit_method``) — y compris en cas d'échec : l'audit porte sur
        l'APPEL, pas seulement sur les succès (S4, tâche 12). La MÊME surface
        d'actions est tracée dans la Flow Map (``_flow_method``) : session
        RICHE pour ``tools/call orchestrate`` — ouverte AVANT le dispatch pour
        capturer les événements du run (``_CURRENT_RECORDER``), clôturée après
        — et MINI-sessions pour les autres actions (``trace_action_flow``).
        """
        flow_token: object | None = None
        recorder: MCPFlowRecorder | None = None  # session orchestrate riche
        if method in _FLOW_METHODS:
            # Contexte d'appel posé pour la durée du dispatch : lu par
            # ``begin_orchestrate_flow`` (session riche) et par les hooks du
            # run (host sortant → ``current_recorder``). Nettoyé en ``finally``.
            flow_token = set_call_context(
                MCPCallContext(client_id=client_id, request_id=_request_run_id(request_id))
            )
            if method == MCPMethod.TOOLS_CALL and params.get("name") == "orchestrate":
                args = self._arguments_or_empty(params)
                recorder = begin_orchestrate_flow(
                    prompt=str(args.get("prompt") or ""),
                    session_id=str(args.get("session_id") or "default"),
                    scope=str(args.get("scope") or "default"),
                )
        try:
            response = self._dispatch_method(
                method, request_id, params, correlation_id=correlation_id
            )
            self._audit_method(
                method, params, response, client_id, request_id, correlation_id=correlation_id
            )
            self._flow_method(method, params, response, client_id, request_id, recorder=recorder)
            return response
        finally:
            if flow_token is not None:
                clear_call_context(flow_token)

    def _flow_method(
        self,
        method: str,
        params: dict[str, Any],
        response: dict[str, Any],
        client_id: str,
        request_id: Any,
        *,
        recorder: MCPFlowRecorder | None,
    ) -> None:
        """Trace l'appel MCP dans le journal « Agent Flow Map » (non bloquant).

        Deux voies, même périmètre que l'audit (catalogue/handshake exclus) :
            - ``tools/call orchestrate`` avec session riche ouverte → clôture
              via ``close_orchestrate_flow`` (statut déduit du résultat) ;
            - toutes les autres actions → ``trace_action_flow`` (mini-session
              ``source=\"mcp\"`` : ``mcp.tool`` ou ``mcp.call``/``mcp.result``).
        Aucune erreur ne remonte : le traçage ne doit jamais altérer la réponse.
        """
        if method not in _FLOW_METHODS:
            return  # catalogue / handshake : même policy que l'audit
        if recorder is not None:
            close_orchestrate_flow(recorder, response)
            return
        if method == MCPMethod.TOOLS_CALL and params.get("name") == "orchestrate":
            # orchestrate sans session riche (flow désactivé / ouverture
            # impossible) : pas de mini-session de repli — le run reste le
            # périmètre de la session riche.
            return
        trace_action_flow(method, params, response, client_id, request_id)

    def _dispatch_method(
        self,
        method: str,
        request_id: Any,
        params: dict[str, Any],
        *,
        correlation_id: str = "",
    ) -> dict[str, Any]:
        """Associe une méthode de requête à son handler (sans audit).

        MCP 2.3.0 — observabilité : ``initialize`` embarque le
        ``correlation_id`` de la requête dans le bloc ``_meta`` du résultat
        (le client relie le handshake à ses logs et à l'audit serveur).
        """
        if method == MCPMethod.INITIALIZE:
            result = self._initialize_result()
            if correlation_id:
                result["_meta"] = build_meta(correlation_id=correlation_id)
            return success_result(request_id, result)
        if method == MCPMethod.PING:
            return success_result(request_id, {})
        if method == MCPMethod.TOOLS_LIST:
            return self._handle_tools_list(request_id, params)
        if method == MCPMethod.TOOLS_CALL:
            return self._handle_tools_call(request_id, params)
        if method == MCPMethod.RESOURCES_LIST:
            return self._handle_resources_list(request_id, params)
        if method == MCPMethod.RESOURCES_READ:
            return self._handle_resources_read(request_id, params)
        if method == MCPMethod.PROMPTS_LIST:
            return self._handle_prompts_list(request_id, params)
        if method == MCPMethod.PROMPTS_GET:
            return self._handle_prompts_get(request_id, params)
        if method == MCPMethod.SAMPLING_CREATE:
            return self._handle_sampling_create(request_id, params)
        raise ProtocolError(ErrorCode.METHOD_NOT_FOUND, f"Method not found: {method}")

    def _handle_notification(self, method: str, *, correlation_id: str = "") -> None:
        """Notifications JSON-RPC : AUCUN acquittement (faible coût, tracé log).

        MCP 2.3.0 — observabilité : le log du ``notifications/initialized``
        porte le ``correlation_id`` de la requête qui l'a déclenchée
        (corrélation logs ↔ handshake ↔ audit).
        """
        if method == MCPMethod.NOTIFICATIONS_INITIALIZED:
            logger.info(
                "MCP client initialized (scope=%s correlation_id=%s)",
                self.scope.value,
                correlation_id or "-",
            )
            return
        logger.info(
            "Notification MCP ignorée : %s (correlation_id=%s)", method, correlation_id or "-"
        )

    # --- Audit MCP (S4, tâche 12) ----------------------------------------------------

    def _audit_method(
        self,
        method: str,
        params: dict[str, Any],
        response: dict[str, Any],
        client_id: str,
        request_id: Any,
        *,
        correlation_id: str = "",
    ) -> None:
        """Journalise un appel MCP d'action via le hook d'audit injecté.

        ``subject`` = ``client_id`` (docs/mcp/MCP_SECURITY.md) ; ``detail`` =
        description de l'appel (tool/URI/prompt, arguments anonymisés par le
        store, ``is_error``, ``scope``) ; ``run_id`` = id JSON-RPC de la
        requête (``mcp_request_id``). L'écriture est NON BLOQUANTE : le hook
        ne doit JAMAIS altérer la réponse MCP.

        MCP 2.3.0 — observabilité : ``detail["correlationId"]`` relie chaque
        entrée d'audit à la requête (logs serveur + réponse client).
        """
        if method == MCPMethod.TOOLS_CALL:
            tool_name = params.get("name")
            action = ACT_MCP_ORCHESTRATE if tool_name == "orchestrate" else ACT_MCP_TOOL_CALL
            # MCP 2.3.0 : métadonnées de retrait du tool appelé (s'il existe et
            # est déprécié) — l'événement d'appel normal EST enrichi et un
            # événement d'audit DÉDIÉ est émis (traçabilité de la migration).
            deprecation = self._deprecation_of(tool_name)
            tool_detail: dict[str, Any] = {
                "method": method,
                "tool": tool_name if isinstance(tool_name, str) else None,
                "arguments": self._arguments_or_empty(params),
                "is_error": self._response_is_error(response),
                "scope": self.scope.value,
            }
            tool_detail.update(deprecation)
            if correlation_id:
                tool_detail["correlationId"] = correlation_id
            self._audit_event(
                action,
                subject=client_id,
                detail=tool_detail,
                run_id=request_id,
            )
            if deprecation.get("deprecated"):
                self._audit_event(
                    ACT_MCP_TOOL_DEPRECATED,
                    subject=client_id,
                    detail=tool_detail,
                    run_id=request_id,
                )
            return
        audit_action = _AUDIT_ACTION_BY_METHOD.get(method)
        if audit_action is None:
            return  # catalogue / handshake : aucune action à auditer
        detail: dict[str, Any]
        if method == MCPMethod.RESOURCES_READ:
            resource_uri = params.get("uri")
            detail = {
                "method": method,
                "uri": resource_uri if isinstance(resource_uri, str) else None,
                "is_error": self._response_is_error(response),
                "scope": self.scope.value,
            }
        elif method == MCPMethod.PROMPTS_GET:
            prompt_name = params.get("name")
            detail = {
                "method": method,
                "prompt": prompt_name if isinstance(prompt_name, str) else None,
                "arguments": self._arguments_or_empty(params),
                "is_error": self._response_is_error(response),
                "scope": self.scope.value,
            }
        elif method == MCPMethod.SAMPLING_CREATE:
            detail = {
                "method": method,
                "sampling_requested": bool(params),
                "is_error": self._response_is_error(response),
                "scope": self.scope.value,
            }
        else:
            return
        if correlation_id:
            # MCP 2.3.0 — observabilité : MÊME traitement que ``tools/call``
            # (resources/read, prompts/get, sampling/create) — toute entrée
            # d'audit d'action porte l'identifiant de corrélation.
            detail["correlationId"] = correlation_id
        self._audit_event(audit_action, subject=client_id, detail=detail, run_id=request_id)

    @staticmethod
    def _arguments_or_empty(params: dict[str, Any]) -> dict[str, Any]:
        """Arguments MCP de l'appel (``None`` / non-objet → ``{}``) pour l'audit."""
        arguments = params.get("arguments")
        return dict(arguments) if isinstance(arguments, dict) else {}

    def _deprecation_of(self, tool_name: Any) -> dict[str, Any]:
        """Métadonnées de retrait (MCP 2.3.0) d'un tool appelé, pour l'audit.

        Args:
            tool_name: nom du tool (``params["name"]``, potentiellement non-str).

        Returns:
            ``{}`` si le tool est inconnu ou non déprécié (détail d'audit
            INCHANGÉ — compatibilité stricte des consommateurs), sinon
            ``{"deprecated": True, "deprecationMessage": ..., "sunsetAt": ...}``
            (les champs absents du manifeste sont omis).
        """
        if not isinstance(tool_name, str):
            return {}
        for tool in self.tool_provider.list_tools():
            if tool.name == tool_name:
                if not tool.deprecated:
                    return {}
                detail: dict[str, Any] = {"deprecated": True}
                if tool.deprecation_message:
                    detail["deprecationMessage"] = tool.deprecation_message
                if tool.sunset_at:
                    detail["sunsetAt"] = tool.sunset_at
                return detail
        return {}

    @staticmethod
    def _response_is_error(response: dict[str, Any]) -> bool:
        """L'appel auditée a-t-il échoué ? (erreur JSON-RPC OU ``isError``)."""
        return bool(response.get("error")) or bool(response.get("result", {}).get("isError"))

    def _audit_event(
        self,
        action: str,
        *,
        subject: str,
        detail: dict[str, Any],
        run_id: Any,
    ) -> Any:
        """Appelle le hook d'audit en isolant TOUTE exception (jamais fatal)."""
        if self.audit is None:
            return None
        try:
            return self.audit(
                action,
                subject=subject,
                detail=detail,
                run_id=_request_run_id(run_id),
            )
        except Exception:  # pragma: no cover - défensif, non bloquant par contrat
            logger.exception("Audit MCP %s impossible (hook) — non bloquant", action)
            return None

    # --- initialize -----------------------------------------------------------------

    def _server_capabilities(self) -> dict[str, dict[str, bool]]:
        """Annonce le support réel, indépendamment de la version du client.

        Les registres permettent la lecture, mais aucun canal de notification
        de catalogue ni abonnement aux ressources n'est câblé : les indicateurs
        restent explicitement faux. Les logs Python et les événements SSE
        d'orchestration ne sont pas des notifications MCP de catalogue/logging.
        ``logging`` est omis : ``logging: {}`` annoncerait un support inexistant
        de ``logging/setLevel`` et ``notifications/message``.

        Une nouvelle projection est construite à chaque handshake, sans état
        partagé ni exigence supplémentaire pour les clients 2.2.x.
        """
        capabilities: dict[str, dict[str, bool]] = {"tools": {"listChanged": False}}
        if self.resource_provider is not None:
            capabilities["resources"] = {"subscribe": False, "listChanged": False}
        if self.prompt_provider is not None:
            capabilities["prompts"] = {"listChanged": False}
        if self.sampling_port is not None:
            # Extension historique conservée pour la compatibilité 2.2.x.
            capabilities["sampling"] = {}
        return capabilities

    def _initialize_result(self) -> dict[str, Any]:
        """Résultat de l'handshake : protocole, capabilities, serverInfo."""
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": self._server_capabilities(),
            "serverInfo": {"name": self.name, "version": str(self.version)},
        }

    # --- sampling/create (tâche 15, S6 v2.0.0) ----------------------------------------

    def _handle_sampling_create(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """``sampling/create`` : reverse LLM inference via ``SamplingPort``.

        Le serveur MCP agit comme CLIENT de son propre LLM : le client MCP
        fournit les messages (``params.messages``) et les préférences
        (``params.maxTokens``, ``params.temperature``, ``params.systemPrompt``).

        Fail-closed : sans ``sampling_port`` (``None``), la méthode est
        rejetée — le serveur ne publie JAMAIS la capacité ``sampling`` tant
        que le port n'est pas injecté.
        """
        if self.sampling_port is None:
            logger.info("MCP sampling/create demandé mais indisponible (v2.0.0)")
            return error_result(
                request_id,
                ErrorCode.INTERNAL_ERROR,
                "sampling/create not available (no SamplingPort wired)",
            )
        # --- Validation des paramètres (spec MCP: createMessageRequest) ----------
        if not isinstance(params, dict) or "messages" not in params:
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                "Invalid params: 'messages' (array) is required",
                data=structured_validation(
                    "Invalid params: 'messages' (array) is required",
                    field_errors={"messages": ["'messages' (array) is required"]},
                ).to_data(),
            )
        raw_messages = params.get("messages")
        if not isinstance(raw_messages, list) or not raw_messages:
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                "Invalid params: 'messages' must be a non-empty array",
            )
        max_tokens = params.get("maxTokens")
        if max_tokens is not None and (not isinstance(max_tokens, int) or max_tokens < 1):
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                "Invalid params: 'maxTokens' must be a positive integer",
            )
        temperature = params.get("temperature")
        if temperature is not None and not isinstance(temperature, (int, float)):
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                "Invalid params: 'temperature' must be a number",
            )
        system_prompt = params.get("systemPrompt")
        if system_prompt is not None and not isinstance(system_prompt, str):
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                "Invalid params: 'systemPrompt' must be a string",
            )
        # --- Construction de la requête de domaine (validation Pydantic) ----------
        try:
            request = SamplingRequest(
                messages=[dict(m) for m in raw_messages],
                max_tokens=max_tokens,
                system_prompt=system_prompt,
                temperature=temperature,
            )
        except ValidationError as exc:
            logger.info("MCP sampling/create params invalides : %s", exc)
            # Contrat 2.3.0 : détail PAR CHAMP (client réparable, non retryable).
            field_errors = {"messages": [str(exc)]}
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                f"Invalid params: {sanitize_message(str(exc))}",
                data=structured_validation(
                    "Invalid sampling request", field_errors=field_errors
                ).to_data(),
            )
        # --- Délégation au port (reverse LLM) -------------------------------------
        try:
            response = self.sampling_port.create_message(request)
        except LLMClientError as exc:
            logger.warning("MCP sampling LLM error : %s", exc)
            # Contrat 2.3.0 : timeout/LLM transient → retryable.
            return error_result(
                request_id,
                ErrorCode.INTERNAL_ERROR,
                f"Sampling LLM error: {sanitize_message(exc.message)}",
                data=structured_timeout("Sampling LLM dependency failed").to_data(),
            )
        except Exception:  # fail-closed : aucune fuite d'exception protocole
            logger.exception("MCP sampling/create a échoué (erreur interne)")
            return error_result(
                request_id,
                ErrorCode.INTERNAL_ERROR,
                "Internal sampling error",
            )
        result = response.to_dict()
        logger.info(
            "MCP sampling/create OK (model=%s, %d chars)",
            result.get("model", ""),
            len(result.get("content", {}).get("text", "")),
        )
        return success_result(request_id, result)

    # --- tools/list & tools/call ------------------------------------------------------

    def _visible_tools(self) -> list[MCPTool]:
        """Tools projetés pour le scope courant (fail-closed via ``granted``)."""
        return [
            tool
            for tool in self.tool_provider.list_tools()
            if self.scope.granted(tool.required_scope)
        ]

    def _handle_tools_list(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """``tools/list`` : catalogue des tools visibles (pagination v2.3.0)."""
        result, error = self._paginated_catalog(
            KIND_TOOLS,
            params,
            [tool.to_dict() for tool in self._visible_tools()],
        )
        if error is not None:
            return error_result(request_id, ErrorCode.INVALID_PARAMS, error)
        return success_result(request_id, {"tools": result[0], **result[1]})

    def _handle_tools_call(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """Exécution d'un tool : validation paramètres → dispatch → isError."""
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                "Invalid params: 'name' (str) is required",
            )
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                "Invalid params: 'arguments' must be an object",
            )
        if name not in {tool.name for tool in self._visible_tools()}:
            # Indiscernable d'un tool absent : aucun oracle de visibilité (scope).
            return error_result(request_id, ErrorCode.INVALID_PARAMS, f"Unknown tool: {name}")
        # MCP 2.3.0 : tool déprécié → l'appel PROCEDE (compatibilité) mais est
        # averti ; l'audit dédié est émis plus bas (tâche 12, _audit_method).
        for tool in self._visible_tools():
            if tool.name == name and tool.deprecated:
                logger.warning(
                    "MCP tool « %s » est DÉPRÉCIÉ (sunset : %s) — %s",
                    name,
                    tool.sunset_at or "date non fixée",
                    tool.deprecation_message or "aucun message de migration",
                )
                break
        try:
            text = self.tool_provider.call_tool(name, dict(arguments or {}))
        except ToolError as exc:
            logger.info("MCP tool %s → isError: %s", name, exc)
            return self._tool_result(request_id, str(exc), is_error=True)
        except Exception:  # fail-closed : aucune fuite d'exception protocole
            logger.exception("MCP tool a échoué (erreur interne)")
            return self._tool_result(request_id, "Internal tool error", is_error=True)
        return self._tool_result(request_id, text, is_error=False)

    # --- resources/list & resources/read (tâche 8) -------------------------------------

    def _handle_resources_list(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """``resources/list`` : catalogue des resources exposées (pagination v2.3.0).

        La liste est rendue par le port (vérité non filtrée) ; le filtrage par
        scope s'ajoutera avec le client store (S4, tâche 11) — toutes les
        resources v1.0.0 sont read-only (visibles de tout rôle).
        """
        if self.resource_provider is None:
            # Aucun registre branché (constructions sur mesure) : surface vide.
            return success_result(request_id, {"resources": []})
        result, error = self._paginated_catalog(
            KIND_RESOURCES,
            params,
            [resource.to_dict() for resource in self.resource_provider.list_resources()],
        )
        if error is not None:
            return error_result(request_id, ErrorCode.INVALID_PARAMS, error)
        return success_result(request_id, {"resources": result[0], **result[1]})

    def _handle_resources_read(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """``resources/read`` : résolution d'une URI → contenu du tool interne."""
        if self.resource_provider is None:
            # Symétrique de « unknown tool » : une surface sans resources est
            # indiscernable d'une URI inconnue (aucun oracle d'implémentation).
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                "Invalid params: no resource registry wired on this server",
            )
        uri = params.get("uri")
        if not isinstance(uri, str) or not uri:
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                "Invalid params: 'uri' (str) is required",
            )
        try:
            text = self.resource_provider.read_resource(uri)
        except NotFoundError as exc:
            # URI inconnue / cible introuvable → erreur de requête (MCP -32602),
            # message actionable préservé (fail-closed, jamais un crash).
            logger.info("MCP resources/read 404 : %s", exc)
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                str(exc),
                data=structured_not_found(str(exc)).to_data(),
            )
        except Exception:  # fail-closed : aucune fuite d'exception protocole
            logger.exception("MCP resources/read a échoué (erreur interne)")
            return error_result(
                request_id,
                ErrorCode.INTERNAL_ERROR,
                "Internal resource error",
                data=structured_internal("Internal resource error").to_data(),
            )
        return success_result(request_id, {"contents": [self._resource_content(uri, text)]})

    def _resource_content(self, uri: str, text: str) -> dict[str, Any]:
        """Contenu MCP ``resources/read`` (``{uri, mimeType?, text}``).

        ``mimeType`` est repris de la métadonnée listée quand l'URI correspond
        exactement (resources statiques) ; les URIs paramétrées n'exposent pas
        de mimeType au bootstrap (champ optionnel de la spec MCP).
        """
        content: dict[str, Any] = {"uri": uri, "text": text}
        provider = self.resource_provider
        if provider is None:  # pragma: no cover - gardé par l'appelant
            return content
        mime = next(
            (
                resource.mime_type
                for resource in provider.list_resources()
                if resource.uri == uri and resource.mime_type
            ),
            None,
        )
        if mime:
            content["mimeType"] = mime
        return content

    # --- prompts/list & prompts/get (tâche 9) ------------------------------------------

    def _handle_prompts_list(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """``prompts/list`` : catalogue des prompts exposés (pagination v2.3.0).

        La liste est rendue par le port (vérité non filtrée) ; le filtrage par
        scope s'ajoutera avec le client store (S4, ``visible_prompts``) — les
        2 prompts v1.0.0 sont des templates statiques (visibles de tout rôle).
        """
        if self.prompt_provider is None:
            # Aucun registre branché (constructions sur mesure) : surface vide.
            return success_result(request_id, {"prompts": []})
        result, error = self._paginated_catalog(
            KIND_PROMPTS,
            params,
            [prompt.to_dict() for prompt in self.prompt_provider.list_prompts()],
        )
        if error is not None:
            return error_result(request_id, ErrorCode.INVALID_PARAMS, error)
        return success_result(request_id, {"prompts": result[0], **result[1]})

    # --- Pagination des catalogues (v2.3.0) ---------------------------------------------

    def _paginated_catalog(
        self,
        kind: str,
        params: dict[str, Any],
        items: list[dict[str, Any]],
    ) -> tuple[tuple[list[dict[str, Any]], dict[str, Any]], str | None]:
        """Découpe un catalogue en page — cœur commun des 3 méthodes ``*/list``.

        Args:
            kind: catalogue émetteur (lie le curseur : ``tools`` / ``resources``
                / ``prompts`` — un curseur croisé est rejeté).
            params: ``params`` JSON-RPC de la requête (lit ``cursor``).
            items: items du catalogue DÉJÀ projetés en dictionnaires MCP.

        Returns:
            ``((page, extra), None)`` où ``extra`` porte ``nextCursor`` (absent
            sur la dernière page), ou ``(..., message)`` si le curseur est
            invalide/expiré (→ ``Invalid params`` -32602, réparable client).

        Compatibilité : sans ``cursor``, page 1 ; avec la taille de page par
        défaut (50) supérieure aux catalogues actuels, la réponse est
        identique aux versions 2.2.x (aucun ``nextCursor`` émis).
        """
        cursor = params.get("cursor")
        offset = 0
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor:
                return (([], {}), "Invalid params: 'cursor' must be an opaque string")
            try:
                offset = decode_cursor(cursor, kind)
            except CursorError as exc:
                prefix = "cursor expired" if exc.expired else "Invalid cursor"
                return (([], {}), f"{prefix}: {exc}")
        page, next_offset = paginate(items, self.page_size, offset)
        extra: dict[str, Any] = (
            {"nextCursor": encode_cursor(kind, next_offset)} if next_offset is not None else {}
        )
        return ((page, extra), None)

    def _handle_prompts_get(self, request_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        """``prompts/get`` : résolution d'un template nommé → messages.

        Validation de forme des paramètres puis délégation au port :
        ``NotFoundError`` (prompt inconnu) et ``ValidationError`` (argument
        requis manquant, valeur non-string) sont des erreurs CLIENT-RÉPARABLES
        → ``Invalid params`` (-32602, message actionable préservé, jamais un
        crash) ; tout le reste est un défaut serveur → ``Internal error``
        (fail-closed, aucune fuite d'exception protocole).
        """
        if self.prompt_provider is None:
            # Symétrique de « unknown tool » : une surface sans prompts est
            # indiscernable d'un prompt inconnu (aucun oracle d'implémentation).
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                "Invalid params: no prompt registry wired on this server",
            )
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                "Invalid params: 'name' (str) is required",
            )
        arguments = params.get("arguments")
        if arguments is not None and not isinstance(arguments, dict):
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                "Invalid params: 'arguments' must be an object",
            )
        try:
            messages = self.prompt_provider.get_prompt(name, dict(arguments or {}))
        except (NotFoundError, ValidationError) as exc:
            # Prompt inconnu / arguments client-réparables → -32602 (MCP) ;
            # le message du domaine est préservé (actionnable).
            logger.info("MCP prompts/get rejeté : %s", exc)
            # Contrat 2.3.0 : prompt inconnu → not_found ; arguments invalides
            # → validation avec détail PAR CHAMP (client réparable).
            if isinstance(exc, NotFoundError):
                contract = structured_not_found(str(exc))
            else:
                contract = structured_validation(str(exc), field_errors={"name": [str(exc)]})
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                str(exc),
                data=contract.to_data(),
            )
        except Exception:  # fail-closed : aucune fuite d'exception protocole
            logger.exception("MCP prompts/get a échoué (erreur interne)")
            return error_result(
                request_id,
                ErrorCode.INTERNAL_ERROR,
                "Internal prompt error",
                data=structured_internal("Internal prompt error").to_data(),
            )
        description = next(
            (
                prompt.description
                for prompt in self.prompt_provider.list_prompts()
                if prompt.name == name
            ),
            None,
        )
        result: dict[str, Any] = {"messages": [message.to_dict() for message in messages]}
        if description:
            # ``description`` est optionnel dans GetPromptResult (spec MCP) :
            # repris de la métadonnée listée pour la complétude du client.
            result["description"] = description
        return success_result(request_id, result)

    # --- Helpers -----------------------------------------------------------------------

    @staticmethod
    def _tool_result(request_id: Any, text: str, *, is_error: bool) -> dict[str, Any]:
        """Réponse MCP ``CallToolResult`` : contenu text + drapeau isError."""
        return success_result(
            request_id,
            {"content": [{"type": "text", "text": text}], "isError": is_error},
        )

    @staticmethod
    def _encode(payload: dict[str, Any] | None) -> str | None:
        if payload is None:
            return None
        return json.dumps(payload, ensure_ascii=False)


__all__ = [
    "InMemoryToolProvider",
    "MCPServer",
    "MCPTool",
    "ToolError",
    "ToolProvider",
]


class InMemoryToolProvider:
    """Registre de tools en mémoire, immutable après construction.

        Bootstrap S1 : suffisant pour ListTools/CallTool en attendant la
    projection du registre legacy ``ToolRegistry`` sur le port
    ``MCPToolRegistryPort`` (S2, tâche 6 : 12 tools read-only).
    """

    def __init__(self, tools: Iterable[MCPTool]) -> None:
        self._tools: dict[str, MCPTool] = {tool.name: tool for tool in tools}

    def list_tools(self) -> list[MCPTool]:
        return list(self._tools.values())

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            raise ToolError(f"Unknown tool: {name}")
        return tool.handler(dict(arguments or {}))
