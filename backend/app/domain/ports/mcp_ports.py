"""Ports MCP — contrats du protocole Model Context Protocol (S1, tâche 3).

Formalise l'interface entre la couche serveur MCP (``app/infrastructure/mcp``)
et le domaine. Le serveur projette ces ports en protocoles JSON-RPC 2.0 :

    tools/list  + tools/call       → MCPToolRegistryPort
    resources/list  + resources/read → MCPResourceRegistryPort
    prompts/list  + prompts/get    → MCPPromptRegistryPort
    sampling/create                → SamplingPort

Chaque port est un Protocol ``runtime_checkable`` : les implémentations
infrastructure (``InMemoryToolProvider``, futur adapter legacy S2, LLM client)
peuvent être vérifiées par ``isinstance`` — fail-fast sur les contrats brisés
(``tests/test_mcp_ports_contract.py``).

Règles d'or :
    - le domaine ne connaît AUCUN transport (SSE, stdio) ni framework (FastAPI) ;
    - le filtrage par scope de sécurité relève de l'infrastructure
      (``MCPServer._visible_tools``), pas du port : le port rend la vérité ;
    - les erreurs métier (404) sont des ``DomainError`` (cf. app/domain/errors.py) ;
    - l'isollement d'erreurs (catch-all autour des handlers) est de la
      responsabilité de l'implémentation, pas du port.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.domain.entities.mcp import (
    MCPPromptMessage,
    MCPPromptTemplate,
    MCPResourceTemplate,
    MCPTool,
)
from app.domain.ports.ports import Message

__all__ = [
    "MCPResourceRegistryPort",
    "MCPPromptRegistryPort",
    "MCPToolRegistryPort",
    "SamplingPort",
]


@runtime_checkable
class MCPToolRegistryPort(Protocol):
    """Contrat de projection des tools MCP (MCP ``tools/list`` + ``tools/call``).

    Remplace le ``ToolProvider`` infrastructurel (docs/mcp/IMPLEMENTATION_PLAN.md,
    tâche 3) : source de vérité des tools exposés, avec scope de sécurité
    (``MCPScopeRole`` intégré à chaque ``MCPTool``).

    Alignement : les méthodes ``list_tools`` / ``call_tool`` de
    ``ToolProvider`` (``mcp_server.py``) — un simple adaptateur projette le
    registre legacy ``ToolRegistry`` (ia/tools/tool_registry.py) sur ce port
    à la S2 (tâche 6 : 12 tools read-only).
    """

    def list_tools(self) -> list[MCPTool]:
        """Tous les tools connus (y compris masqués par le scope).

        Le filtrage par scope (``required_scope`` vs rôle client) relève de
        l'infrastructure (``MCPServer._visible_tools``) : le port rend la
        vérité, le serveur projette la vue sécurisée.
        """
        ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Exécute un tool par son nom.

        Lève ``ToolError`` (erreur métier → ``isError: true``) pour un tool
        connu qui échoue ; un nom inconnu est traité comme ``Invalid Params``
        par le serveur. L'isollement d'erreurs (catch-all) est de la
        responsabilité de l'implémentation.
        """
        ...


@runtime_checkable
class MCPResourceRegistryPort(Protocol):
    """Contrat des ressources MCP (MCP ``resources/list`` + ``resources/read``).

    'URI templates ↔ tools' : chaque template ``thinktuning://job/{job_id}``
    est résolu à la lecture par ``read_resource`` (potentiellement via un tool
    backend). La liste des templates sert à ``resources/templates`` dans
    l'initialisation du handshake MCP.

    La vraie implémentation arrive en S3 (tâche 8 : 5 resources statiques) et
    S5 (tâche 11 : 10 resources) ; le port est défini maintenant pour que le
    serveur sache où brancher le registre dès qu'il est prêt.
    """

    def list_resources(self) -> list[MCPResourceTemplate]:
        """Templates URI exposés (ressources statiques + templates dynamiques)."""
        ...

    def read_resource(self, uri: str) -> str:
        """Résout une URI concrète en contenu texte.

        Lève ``NotFoundError`` (404) si l'URI est inconnue ou non autorisée
        pour le scope du client. L'anti-SSRF et la validation de chemin
        restent de la responsabilité de l'implémentation.
        """
        ...


@runtime_checkable
class MCPPromptRegistryPort(Protocol):
    """Contrat des prompts MCP (MCP ``prompts/list`` + ``prompts/get``).

    Un prompt est un template nommé : ``list_prompts`` rend le catalogue ;
    ``get_prompt`` résout les arguments en messages (role + content).

    La vraie implémentation arrive en S3 (tâche 9 : 2 prompts) ; le port
    est défini maintenant pour valider le contrat avant l'implémentation.
    """

    def list_prompts(self) -> list[MCPPromptTemplate]:
        """Catalogue des prompts statiques exposés."""
        ...

    def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> list[MCPPromptMessage]:
        """Résout un prompt en messages (role + content).

        Lève ``NotFoundError`` (404) si le nom est inconnu ; lève
        ``ValidationError`` (422) si un argument requis
        (``MCPPromptArgument.required``) est manquant.
        """
        ...


@runtime_checkable
class SamplingPort(Protocol):
    """Reverse LLM inference — MCP ``sampling/create``.

    Le serveur MCP agit comme CLIENT de son propre LLM sur demande d'un client
    MCP : celui-ci fournit les messages et les préférences, le serveur génère
    du texte via ``SamplingPort``.

    C'est la couche d'abstraction entre le transport MCP (``sampling/create``)
    et l'implémentation LLM (``HttpLLMClient``, ``StubLLMClient``) : un client
    ``LLMClientPort`` compatible peut implémenter ce port pour exposer le
    sampling. Le scope ``sampling_enabled`` (S4, MCP_SECURITY.md) contrôle
    l'accès.

    La vraie implémentation arrive en S6 (tâche 15 : ``SamplingPort`` +
    orchestrate tool).
    """

    def create_text(
        self,
        messages: list[Message],
        *,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Génère du texte depuis une liste de messages.

        Lève ``LLMClientError`` (domaine) en cas d'échec du provider LLM ; le
        serveur MCP traduit en ``error`` JSON-RPC (code -32603).
        """
        ...
