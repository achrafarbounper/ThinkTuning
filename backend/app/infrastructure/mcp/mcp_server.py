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
from core.audit_store import (  # S4, tâche 12 : actions d'audit normalisées MCP
    ACT_MCP_ORCHESTRATE,
    ACT_MCP_PROMPT_GET,
    ACT_MCP_RESOURCE_READ,
    ACT_MCP_SAMPLING,
    ACT_MCP_TOOL_CALL,
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


def _request_run_id(request_id: Any) -> str | None:
    """``run_id`` MCP : id JSON-RPC normalisé en str (``mcp_request_id``)."""
    if request_id is None:
        return None
    return str(request_id)


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

    # --- Surface publique --------------------------------------------------------

    def handle_text(self, raw: str, *, client_id: str = "anonymous") -> str | None:
        """Parse un message JSON-RPC (texte brut) et retourne la réponse encodée.

        Args :
            raw : corps JSON-RPC (texte) ;
            client_id : identité du client MCP appelant — portée en
                ``subject`` de chaque entrée d'audit produite par cet appel
                (le transport la résout depuis son en-tête / sa session).

        Returns:
            La réponse JSON-RPC sérialisée à émettre, ou ``None`` pour une
            notification (MCP : aucune réponse attendue sur le transport).
        """
        try:
            payload = parse_jsonrpc(raw)
        except ProtocolError as exc:
            return self._encode(error_result(None, exc.code, exc.message))
        try:
            response = self._dispatch(payload, client_id=client_id)
        except ProtocolError as exc:
            response = error_result(None, exc.code, exc.message)
        return self._encode(response)

    # --- Dispatch -------------------------------------------------------------------

    def _dispatch(
        self, payload: dict[str, Any] | list[Any], *, client_id: str
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
            self._handle_notification(method)
            return None
        params = payload.get("params", {})
        request_id = payload.get("id")
        if not isinstance(params, dict):
            params = {}
        return self._handle_method(method, request_id, params, client_id)

    def _handle_method(
        self,
        method: str,
        request_id: Any,
        params: dict[str, Any],
        client_id: str,
    ) -> dict[str, Any]:
        """Dispatch d'une méthode de REQUÊTE (id présent) → réponse JSON-RPC.

        Chaque méthode d'ACTION (tools/call, resources/read, prompts/get,
        sampling/create) est ensuite AUDITÉE de façon centralisée
        (``_audit_method``) — y compris en cas d'échec : l'audit porte sur
        l'APPEL, pas seulement sur les succès (S4, tâche 12).
        """
        response = self._dispatch_method(method, request_id, params)
        self._audit_method(method, params, response, client_id, request_id)
        return response

    def _dispatch_method(
        self, method: str, request_id: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Associe une méthode de requête à son handler (sans audit)."""
        if method == MCPMethod.INITIALIZE:
            return success_result(request_id, self._initialize_result())
        if method == MCPMethod.PING:
            return success_result(request_id, {})
        if method == MCPMethod.TOOLS_LIST:
            return success_result(
                request_id, {"tools": [tool.to_dict() for tool in self._visible_tools()]}
            )
        if method == MCPMethod.TOOLS_CALL:
            return self._handle_tools_call(request_id, params)
        if method == MCPMethod.RESOURCES_LIST:
            return self._handle_resources_list(request_id)
        if method == MCPMethod.RESOURCES_READ:
            return self._handle_resources_read(request_id, params)
        if method == MCPMethod.PROMPTS_LIST:
            return self._handle_prompts_list(request_id)
        if method == MCPMethod.PROMPTS_GET:
            return self._handle_prompts_get(request_id, params)
        if method == MCPMethod.SAMPLING_CREATE:
            return self._handle_sampling_create(request_id, params)
        raise ProtocolError(ErrorCode.METHOD_NOT_FOUND, f"Method not found: {method}")

    def _handle_notification(self, method: str) -> None:
        """Notifications JSON-RPC : AUCUN acquittement (faible coût, tracé log)."""
        if method == MCPMethod.NOTIFICATIONS_INITIALIZED:
            logger.info("MCP client initialized (scope=%s)", self.scope.value)
            return
        logger.info("Notification MCP ignorée : %s", method)

    # --- Audit MCP (S4, tâche 12) ----------------------------------------------------

    def _audit_method(
        self,
        method: str,
        params: dict[str, Any],
        response: dict[str, Any],
        client_id: str,
        request_id: Any,
    ) -> None:
        """Journalise un appel MCP d'action via le hook d'audit injecté.

        ``subject`` = ``client_id`` (docs/mcp/MCP_SECURITY.md) ; ``detail`` =
        description de l'appel (tool/URI/prompt, arguments anonymisés par le
        store, ``is_error``, ``scope``) ; ``run_id`` = id JSON-RPC de la
        requête (``mcp_request_id``). L'écriture est NON BLOQUANTE : le hook
        ne doit JAMAIS altérer la réponse MCP.
        """
        if method == MCPMethod.TOOLS_CALL:
            tool_name = params.get("name")
            action = ACT_MCP_ORCHESTRATE if tool_name == "orchestrate" else ACT_MCP_TOOL_CALL
            self._audit_event(
                action,
                subject=client_id,
                detail={
                    "method": method,
                    "tool": tool_name if isinstance(tool_name, str) else None,
                    "arguments": self._arguments_or_empty(params),
                    "is_error": self._response_is_error(response),
                    "scope": self.scope.value,
                },
                run_id=request_id,
            )
            return
        action = _AUDIT_ACTION_BY_METHOD.get(method)
        if action is None:
            return  # catalogue / handshake : aucune action à auditer
        if method == MCPMethod.RESOURCES_READ:
            uri = params.get("uri")
            detail = {
                "method": method,
                "uri": uri if isinstance(uri, str) else None,
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
        self._audit_event(action, subject=client_id, detail=detail, run_id=request_id)

    @staticmethod
    def _arguments_or_empty(params: dict[str, Any]) -> dict[str, Any]:
        """Arguments MCP de l'appel (``None`` / non-objet → ``{}``) pour l'audit."""
        arguments = params.get("arguments")
        return dict(arguments) if isinstance(arguments, dict) else {}

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

    def _initialize_result(self) -> dict[str, Any]:
        """Résultat de l'handshake : protocole, capabilities, serverInfo."""
        capabilities: dict[str, Any] = {"tools": {"listChanged": False}}
        if self.resource_provider is not None:
            # Tâche 8 : la surface expose des resources → capability annoncée.
            capabilities["resources"] = {"subscribe": False, "listChanged": False}
        if self.prompt_provider is not None:
            # Tâche 9 : la surface expose des prompts → capability annoncée.
            capabilities["prompts"] = {"listChanged": False}
        if self.sampling_port is not None:
            # Tâche 15 (v2.0.0) : capacité sampling annoncée → clients doivent
            # mettre à jour pour gérer ``sampling/create`` (breaking change).
            capabilities["sampling"] = {}
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": capabilities,
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
            return error_result(
                request_id,
                ErrorCode.INVALID_PARAMS,
                f"Invalid params: {exc}",
            )
        # --- Délégation au port (reverse LLM) -------------------------------------
        try:
            response = self.sampling_port.create_message(request)
        except LLMClientError as exc:
            logger.warning("MCP sampling LLM error : %s", exc)
            return error_result(
                request_id,
                ErrorCode.INTERNAL_ERROR,
                f"Sampling LLM error: {exc.message}",
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

    def _handle_resources_list(self, request_id: Any) -> dict[str, Any]:
        """``resources/list`` : catalogue des resources exposées (métadonnées).

        La liste est rendue par le port (vérité non filtrée) ; le filtrage par
        scope s'ajoutera avec le client store (S4, tâche 11) — toutes les
        resources v1.0.0 sont read-only (visibles de tout rôle).
        """
        if self.resource_provider is None:
            # Aucun registre branché (constructions sur mesure) : surface vide.
            return success_result(request_id, {"resources": []})
        return success_result(
            request_id,
            {
                "resources": [
                    resource.to_dict() for resource in self.resource_provider.list_resources()
                ]
            },
        )

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
            return error_result(request_id, ErrorCode.INVALID_PARAMS, str(exc))
        except Exception:  # fail-closed : aucune fuite d'exception protocole
            logger.exception("MCP resources/read a échoué (erreur interne)")
            return error_result(request_id, ErrorCode.INTERNAL_ERROR, "Internal resource error")
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

    def _handle_prompts_list(self, request_id: Any) -> dict[str, Any]:
        """``prompts/list`` : catalogue des prompts exposés (métadonnées pures).

        La liste est rendue par le port (vérité non filtrée) ; le filtrage par
        scope s'ajoutera avec le client store (S4, ``visible_prompts``) — les
        2 prompts v1.0.0 sont des templates statiques (visibles de tout rôle).
        """
        if self.prompt_provider is None:
            # Aucun registre branché (constructions sur mesure) : surface vide.
            return success_result(request_id, {"prompts": []})
        return success_result(
            request_id,
            {"prompts": [prompt.to_dict() for prompt in self.prompt_provider.list_prompts()]},
        )

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
            return error_result(request_id, ErrorCode.INVALID_PARAMS, str(exc))
        except Exception:  # fail-closed : aucune fuite d'exception protocole
            logger.exception("MCP prompts/get a échoué (erreur interne)")
            return error_result(request_id, ErrorCode.INTERNAL_ERROR, "Internal prompt error")
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
