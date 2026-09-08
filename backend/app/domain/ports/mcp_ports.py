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

from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.domain.entities.mcp import (
    MCPPromptMessage,
    MCPPromptTemplate,
    MCPResource,
    MCPTool,
    SamplingRequest,
    SamplingResponse,
)
from app.domain.ports.ports import Message

__all__ = [
    "MCPResourceRegistryPort",
    "MCPPromptRegistryPort",
    "MCPSecurityScope",
    "MCPToolRegistryPort",
    "SamplingPort",
    "SamplingRequest",
    "SamplingResponse",
    "_sampling_create_text",
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

    « URI templates ↔ tools » : chaque template ``thinktuning://jobs/{job_id}``
    est résolu à la lecture par ``read_resource`` (via un tool backend). La
    liste des resources sert à ``resources/list`` dans l'initialisation du
    handshake MCP ; les gabarits paramétrés sont distingués côté provider
    (``list_resource_templates``, préparation ``resources/templates/list``).

    Implémentation livrée en S3 (tâche 8 : 5 resources ``thinktuning://`` via
    ``LegacyResourceProvider``) ; extension en S5 (tâche 11 : 10 resources).

    Règles :
        - la liste est de la MÉTADONNÉE pure (aucune I/O) : le catalogue se
          construit sans toucher aux tools backend ;
        - ``read_resource`` lève ``NotFoundError`` (404) si l'URI est inconnue
          ou si la cible l'est (job/dataset absent, chemin hors sandbox) — le
          serveur traduit en erreur JSON-RPC ; l'anti-traversée et la lecture
          seule (``safe_resolve``, SQLite ``query_only``) restent portées par
          l'implémentation (délégation aux tools legacy).
    """

    def list_resources(self) -> list[MCPResource]:
        """Resources exposées (statiques + gabarits des paramétrées)."""
        ...

    def read_resource(self, uri: str) -> str:
        """Résout une URI concrète en contenu texte (JSON sérialisé).

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
    """Reverse LLM inference — MCP ``sampling/create`` (tache 15, S6 v2.0.0).

    Le serveur MCP agit comme CLIENT de son propre LLM sur demande d'un
    client MCP : celui-ci fournit les messages et les preferences, le
    serveur genere du texte via ``SamplingPort``.

    Deux niveaux (compatibilite S1 -> S6) :
      - ``create_message(request)`` — contrat CANONIQUE (tache 15) :
        entree validee ``SamplingRequest`` -> sortie typee
        ``SamplingResponse`` ;
      - ``create_text(...)`` — commodite S1 (``str`` direct) ; les
        implementations DOIVENT le fournir par delegation a
        ``create_message`` (defaut via ``_sampling_create_text``).

    Le scope ``sampling_enabled`` (S4, MCP_SECURITY.md) controle l'acces ;
    ``LLMClientError`` (domaine) en cas d'echec provider -> le serveur MCP
    traduit en ``error`` JSON-RPC (code -32603).
    """

    def create_message(self, request: SamplingRequest) -> SamplingResponse:
        """Genere une completion typee depuis une requete validee."""
        ...

    def create_text(
        self,
        messages: list[Message],
        *,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Genere du texte brut (commodite deleguant a ``create_message``)."""
        ...


def _sampling_create_text(
    port: SamplingPort,
    messages: list[Message],
    *,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
    temperature: float | None = None,
) -> str:
    """Implémentation par défaut de ``create_text`` via ``create_message``.

    Construit la ``SamplingRequest`` (validation Pydantic fail-fast),
    délègue au contrat canonique, et ne rend que ``response.text``.
    Factorisée ici pour que chaque adaptateur l'utilise sans duplication.
    """
    request = SamplingRequest(
        messages=[dict(m) for m in messages],
        max_tokens=max_tokens,
        system_prompt=system_prompt,
        temperature=temperature,
    )
    return port.create_message(request).text


# ============================================================
# MCP SECURITY SCOPE  (S4 — Tâche 10)
# ============================================================
# Modèle de domaine PUR (Pydantic v2, frozen, extra="forbid") :
# définit le périmètre d'autorisation d'un client MCP (client_id,
# tenant, rôle, listes de visibilité, quotas, révocation).
#
# Ce modèle est la SOURCE DE VÉRITÉ du scope : il est produit par
# ``MCPClientStore.register`` et consommé par l'infrastructure
# (``scope_enforcer.py``) pour filtrer tools / resources / prompts
# et appliquer les quotas. Le domaine ne connaît pas le transport
# ni le framework — juste la définition du scope.


class MCPSecurityScope(BaseModel):
    """Périmètre de sécurité d'un client MCP (docs/mcp/MCP_SECURITY.md).

    Chaque client MCP est **toujours** associé à un scope. Le scope limite
    ce que le client peut voir (tools, resources, prompts) et faire
    (sampling, quotas destructifs, limite de débit).

    Attributs :
        client_id : identifiant unique du client MCP (ex. ``"claude-desktop-prod"``).
        tenant_id : tenant / environnement (``"default"``, ``"staging"``, ``"production"``).
        role : rôle de sécurité (``MCPScopeRole``) — ordonné du plus restrictif
            au plus permissif : ``read_only`` < ``contributor`` < ``operator`` < ``admin``.
        visible_tools : whitelist des noms de tools visibles (vide = tous les tools
            dont le ``required_scope`` ≤ rôle du client, filtré à l'infrastructure).
        visible_resources : whitelist des URI patterns ou noms de resources visibles.
        visible_prompts : whitelist des noms de prompts visibles.
        sampling_enabled : autorise le MCP ``sampling/create`` (``False`` par défaut).
        rate_limit_per_minute : débit maximal (appels/min) — 60 par défaut, 600 admin,
            1200 CI.
        destructive_quota : nombre maximal d'outils "manual approval" / heure
            (5 par défaut) — les tools marqués ``destructiveHint`` passent par
            cette quota.
        revoked : le client a été révoqué (accès immédiatement refusé, HTTP 401).
        revoked_at : horodatage UTC de la révocation (``None`` si non révoqué).
        revoked_reason : motif de la révocation (ex. ``"compromised_token"``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    client_id: str = Field(
        ...,
        min_length=1,
        description="Identifiant unique du client MCP (ex. 'claude-desktop-prod').",
    )
    tenant_id: str = Field(
        default="default",
        min_length=1,
        description="Tenant / environnement ('default', 'staging', 'production').",
    )
    role: str = Field(
        ...,
        description="Rôle de sécurité ('read_only', 'contributor', 'operator', 'admin').",
    )
    visible_tools: list[str] = Field(
        default_factory=list,
        description="Whitelist des tools visibles (vide = tous autorisés par rôle).",
    )
    visible_resources: list[str] = Field(
        default_factory=list,
        description="Whitelist des URI patterns / noms de resources visibles.",
    )
    visible_prompts: list[str] = Field(
        default_factory=list,
        description="Whitelist des noms de prompts visibles.",
    )
    sampling_enabled: bool = Field(
        default=False,
        description="Autorise MCP sampling/create (False par défaut, True pour operator+).",
    )
    rate_limit_per_minute: int = Field(
        default=60,
        ge=1,
        le=10000,
        description="Débit maximal (appels/min) : 60 default, 600 admin, 1200 CI.",
    )
    destructive_quota: int = Field(
        default=5,
        ge=0,
        le=1000,
        description="Quota max d'outils 'manual approval' / heure (5 default).",
    )
    revoked: bool = Field(
        default=False,
        description="Le client est révoqué (accès immédiatement refusé, HTTP 401).",
    )
    revoked_at: datetime | None = Field(
        default=None,
        description="Horodatage UTC de la révocation (None si non révoqué).",
    )
    revoked_reason: str = Field(
        default="",
        description="Motif de la révocation (ex. 'compromised_token').",
    )

    # --- Helpers de domaine ---------------------------------------------------

    @property
    def is_active(self) -> bool:
        """Le scope est-il actif (non révoqué) ?"""
        return not self.revoked

    def revoke(self, reason: str, *, at: datetime | None = None) -> MCPSecurityScope:
        """Retourne UNE NOUVELLE instance de scope révoqué (immuable).

        Le scope étant ``frozen``, la révocation produit une copie avec
        ``revoked=True``, ``revoked_at`` et ``revoked_reason`` renseignés.
        Les métadonnées de révocation sont validées (raison non vide).
        """
        if not reason or not reason.strip():
            raise ValueError("Le motif de révocation ne peut pas être vide.")
        now = at or datetime.now(UTC)
        return self.model_copy(
            update={
                "revoked": True,
                "revoked_at": now,
                "revoked_reason": reason.strip(),
            }
        )

    def to_dict(self) -> dict[str, Any]:
        """Représentation sérialisable (ISO 8601 pour ``revoked_at``)."""
        d = self.model_dump()
        if d.get("revoked_at") is not None:
            d["revoked_at"] = d["revoked_at"].isoformat()
        return d
