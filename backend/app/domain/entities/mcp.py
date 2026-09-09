# project/app/domain/entities/mcp.py
"""Entité de versionnement de la surface MCP (Model Context Protocol).

``MCPVersion`` est la source de vérité du numéro de version du serveur MCP
(SSE + stdio, tâche 2). La feuille de route MCP (``docs/mcp/IMPLEMENTATION_PLAN.md``)
cadence les livraisons par version, comparables pour gater les features :

    S1 bootstrap → v0.1.0 · S3 beta → v1.0.0 · S5 → v1.1.0
    S6 sampling  → v2.0.0 · S7 MCP-first → v3.0.0

Règles :
    - Modèle de domaine PUR : aucune I/O fichier, aucune dépendance FastAPI,
      legacy ou LLM. La lecture de ``pyproject.toml`` (``[tool.mcp]``) vit dans
      l'infrastructure (``app/infrastructure/mcp/version_loader.py``), qui
      délègue le parsing pur à ``MCPVersion.from_toml_source`` ;
    - Pydantic v2, immuable (frozen), sémantique strictement ``X.Y.Z``
      (pas de pré-release : la roadmap n'utilise que des versions stables).
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Sémantique X.Y.Z stricte (semver sans pré-release ni build) :
#   - exactement 3 composants numériques ("1.2", "1.2.3.4" rejetés) ;
#   - pas de zéro initial ("01.2.3" rejeté, cf. semver.org §2).
_SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class MCPVersion(BaseModel):
    """Version de la surface MCP, sémantique stricte ``major.minor.patch``.

    Value object immuable et ordonnable : les jalons de la feuille de route
    MCP se comparent naturellement pour conditionner l'exposition des tools /
    resources / prompts (ex. ``v.parse("1.0.0") >= MCPVersion.parse("1.0.0")``).

    Attributs :
        major:  composant majeur (>= 0) — breaking changes du protocole ;
        minor:  composant mineur (>= 0) — ajout de features rétrocompatibles ;
        patch:  composant de correctif (>= 0).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    major: int = Field(ge=0)
    minor: int = Field(ge=0)
    patch: int = Field(ge=0)

    # --- Fabriques -----------------------------------------------------------

    @classmethod
    def parse(cls, raw: str) -> MCPVersion:
        """Parse une version ``"X.Y.Z"`` stricte ou lève ``ValueError``.

        Tolère les espaces englobants (valeurs de config) ; tout le reste est
        refusé avec un message actionnable (fail-fast à la frontière domaine).
        """
        if not isinstance(raw, str):
            raise ValueError(
                "Version MCP invalide : attendu 'X.Y.Z' (str), "
                f"reçu {type(raw).__name__}"
            )
        match = _SEMVER_PATTERN.match(raw.strip())
        if match is None:
            raise ValueError(
                f"Version MCP invalide : '{raw}' (format attendu 'major.minor.patch', "
                "entiers >= 0 sans zéro initial, ex. '0.1.0')"
            )
        major, minor, patch = (int(group) for group in match.groups())
        return cls(major=major, minor=minor, patch=patch)

    @classmethod
    def from_toml_source(cls, source: str) -> MCPVersion:
        """Extrait ``[tool.mcp] version`` d'un contenu TOML (pur, sans I/O).

        Lève ``ValueError`` si le TOML est invalide, si la table ``[tool.mcp]``
        ou la clé ``version`` manque, ou si la valeur n'est pas un ``X.Y.Z``
        valide. La résolution du fichier + fallback tolérant relèvent de
        l'infrastructure (``version_loader.load_mcp_version``).
        """
        try:
            data = tomllib.loads(source)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"pyproject.toml : TOML invalide ({exc})") from exc
        table = data.get("tool", {}).get("mcp")
        if not isinstance(table, dict) or "version" not in table:
            raise ValueError(
                "pyproject.toml : table [tool.mcp] absente ou clé 'version' manquante"
            )
        version = table["version"]
        if not isinstance(version, str):
            raise ValueError(
                "pyproject.toml : [tool.mcp] version doit être une chaîne, "
                f"reçu {type(version).__name__}"
            )
        return cls.parse(version)

    # --- Sérialisation / comparaison ------------------------------------------

    def as_tuple(self) -> tuple[int, int, int]:
        """Triplet ``(major, minor, patch)`` — base du tri et des gates."""
        return (self.major, self.minor, self.patch)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, MCPVersion):
            return NotImplemented
        return self.as_tuple() < other.as_tuple()

    def __le__(self, other: object) -> bool:
        if not isinstance(other, MCPVersion):
            return NotImplemented
        return self.as_tuple() <= other.as_tuple()

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, MCPVersion):
            return NotImplemented
        return self.as_tuple() > other.as_tuple()

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, MCPVersion):
            return NotImplemented
        return self.as_tuple() >= other.as_tuple()


# Version livrée par le bootstrap S1 (docs/mcp/IMPLEMENTATION_PLAN.md) —
# fallback du loader quand pyproject.toml est absent ou inexploitable.
DEFAULT_MCP_VERSION = MCPVersion(major=2, minor=0, patch=0)


class MCPScopeRole(StrEnum):
    """Rôle de sécurité d'un client MCP (docs/mcp/MCP_SECURITY.md).

    Les rôles sont ORDONNÉS du plus restrictif au plus permissif :

        read_only (12 tools lecture) < contributor (25 tools + write filtré)
        < operator (35 tools + exec filtré) < admin (40 tools full access)

    ``granted`` conditionne la visibilité d'un tool pendant le bootstrap S1
    (tâche 2 : ``build_mcp_server(scope=...)``). Le scope CLIENT complet
    (``MCPSecurityScope`` : client_id, visible_tools, quotas, révocation…)
    arrive avec le client store de la S4 — il s'appuiera sur ce rôle.
    """

    READ_ONLY = "read_only"
    CONTRIBUTOR = "contributor"
    OPERATOR = "operator"
    ADMIN = "admin"

    def granted(self, required: MCPScopeRole | str) -> bool:
        """Le rôle courant couvre-t-il un élément exigeant ``required`` ?

        Fail-closed : un ``required`` inconnu lève ``ValueError`` (un rôle
        inexistant ne doit jamais être contourné par un simple ``granted``
        comparant des chaînes).
        """
        if not isinstance(required, MCPScopeRole):
            required = MCPScopeRole(required)
        return _SCOPE_ROLE_RANK[self] >= _SCOPE_ROLE_RANK[required]

    @property
    def rank(self) -> int:
        """Ordre de privilège (0 = read_only → 3 = admin)."""
        return _SCOPE_ROLE_RANK[self]


_SCOPE_ROLE_RANK = {
    MCPScopeRole.READ_ONLY: 0,
    MCPScopeRole.CONTRIBUTOR: 1,
    MCPScopeRole.OPERATOR: 2,
    MCPScopeRole.ADMIN: 3,
}


# ============================================================
# MCP TOOLS & SUPPORTING ENTITIES  (S1 — Bootstrap)
# ============================================================
#
# Ces entités sont PURES : aucune I/O, aucune dépendance framework.
# Elles vivent dans le domaine car décrites par les ports MCP
# (app/domain/ports/mcp_ports.py) et projetées par le serveur MCP
# (app/infrastructure/mcp/mcp_server.py). Le déplacement de ``MCPTool``
# de l'infrastructure vers le domaine est la formalisation demandée par
# la tâche 3 : le domaine est la source de vérité, pas l'infrastructure.
#


@dataclass(frozen=True, slots=True)
class MCPTool:
    """Métadonnées + handler d'un tool exposé via MCP.

    Value object immuable — le domaine ne connaît ni transport, ni sandbox.
    La projection MCP (``to_dict``) est pure : l'infrastructure appelle
    ``to_dict`` pour construire la réponse ``tools/list``.

    Attributs :
        name :           identifiant MCP unique du tool ;
        description :    description lisible (listée dans tools/list) ;
        input_schema :   JSON Schema des arguments (``inputSchema`` MCP) ;
        annotations :    ``annotations`` MCP — readOnlyHint / destructiveHint /
                         idempotentHint. Projetées DESIGN-TIME depuis la
                         politique ``thinktuning.tool/v1`` (safety) par
                         ``manifest_generator`` (tâche 4) et réévaluées
                         RUNTIME par appel via le ``policy_adapter`` (tâche 5) ;
        required_scope : rôle minimal pour VOIR et APPELER le tool
                         (``MCPScopeRole``, docs/mcp/MCP_SECURITY.md) ;
        handler :        exécution pure ``(arguments: dict) -> str`` ; lève
                         ``ToolError`` pour une erreur métier (isError).
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, bool]
    required_scope: MCPScopeRole
    handler: Callable[[dict[str, Any]], str]

    def to_dict(self) -> dict[str, Any]:
        """Projection MCP du tool (``tools/list``)."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "annotations": self.annotations,
        }


@dataclass(frozen=True, slots=True)
class MCPResource:
    """Ressource MCP exposée via ``resources/list`` (tâche 8).

    Value object immuable — le domaine ne connaît ni transport, ni tool, ni
    sandbox. La projection MCP (``to_dict``) est pure : l'infrastructure
    appelle ``to_dict`` pour construire la réponse ``resources/list``.

    Aligné sur la spec MCP ``resources/list`` (uri + name obligatoires,
    description et mimeType optionnels).

    Attributs :
        uri :         URI canonique de la resource (schéma ``thinktuning://``) ;
                      pour les resources PARAMÉTRÉES, l'URI listée est le
                      gabarit (ex. ``thinktuning://jobs/{job_id}``) — la
                      résolution d'une URI concrète est déléguée au registre
                      (``MCPResourceRegistryPort.read_resource``) ;
        name :        identifiant lisible (ex. ``jobs``, ``config``) ;
        description : description humaine (listée dans resources/list) ;
        mime_type :   type MIME du contenu lu (``application/json`` pour les
                      5 resources ThinkTuning v1.0.0).
    """

    uri: str
    name: str
    description: str = ""
    mime_type: str = "application/json"

    def to_dict(self) -> dict[str, Any]:
        """Projection MCP de la resource (``resources/list``)."""
        result: dict[str, Any] = {"uri": self.uri, "name": self.name}
        if self.description:
            result["description"] = self.description
        if self.mime_type:
            result["mimeType"] = self.mime_type
        return result


@dataclass(frozen=True)
class MCPPromptArgument:
    """Argument d'un prompt-resource template (MCP ``prompts/arguments``).

    ``description`` est optionnelle (MCP la rend optionnelle dans la spec) ;
    ``required`` vaut ``False`` par défaut (convention projet : opt-in explicite).
    """

    name: str
    description: str = ""
    required: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Projection MCP de l'argument."""
        result: dict[str, Any] = {"name": self.name}
        if self.description:
            result["description"] = self.description
        result["required"] = self.required
        return result


@dataclass(frozen=True)
class MCPResourceTemplate:
    """Template de ressource MCP — URI template ↔ tool backend.

    Déclare un URI template (``thinktuning://job/{job_id}``) dont la résolution
    est déléguée à ``MCPResourceRegistryPort.read_resource`` (potentiellement
    via un tool backend). Servira à ``resources/list`` /
    ``resources/templates/list`` (roadmap v1.0+).

    Aligné sur la spec MCP ``resources/templates/list``.
    """

    uri_template: str
    name: str
    description: str = ""
    mime_type: str = "text/plain"
    arguments: tuple[MCPPromptArgument, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Projection MCP du template (``resources/templates``)."""
        result: dict[str, Any] = {
            "uriTemplate": self.uri_template,
            "name": self.name,
        }
        if self.description:
            result["description"] = self.description
        if self.mime_type:
            result["mimeType"] = self.mime_type
        if self.arguments:
            result["arguments"] = [a.to_dict() for a in self.arguments]
        return result


@dataclass(frozen=True)
class MCPPromptTemplate:
    """Template de prompt MCP (``prompts/list``).

    ``name`` + ``description`` + ``arguments`` → catalogue pour
    ``prompts/get`` : le client fournit les arguments, le serveur résout le
    template en messages (role + content).

    Aligné sur la spec MCP ``prompts/list``.
    """

    name: str
    description: str = ""
    arguments: tuple[MCPPromptArgument, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Projection MCP du prompt (``prompts/list``)."""
        result: dict[str, Any] = {"name": self.name}
        if self.description:
            result["description"] = self.description
        if self.arguments:
            result["arguments"] = [a.to_dict() for a in self.arguments]
        return result


@dataclass(frozen=True)
class MCPPromptMessage:
    """Message d'un prompt résolu (``prompts/get`` → ``messages``).

    ``content`` est le texte brut du message ; ``to_dict`` projette vers le
    format MCP ``{role, content: {type: "text", text}}``.
    """

    role: str
    content: str

    def to_dict(self) -> dict[str, Any]:
        """Projection MCP du message (``prompts/get`` response)."""
        return {
            "role": self.role,
            "content": {"type": "text", "text": self.content},
        }


class SamplingRequest(BaseModel):
    """Requete de sampling MCP (``sampling/createMessage`` -> LLM)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: list[dict[str, Any]] = Field(
        ...,
        min_length=1,
        description="Historique OpenAI : [{'role': ..., 'content': ...}] (min 1).",
    )
    max_tokens: int | None = Field(default=None, ge=1, le=100000)
    system_prompt: str | None = Field(default=None)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)

    @field_validator("messages")
    @classmethod
    def _validate_messages(
        cls, value: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        for index, message in enumerate(value):
            if not isinstance(message, dict):
                raise ValueError(f"Message #{index} : objet attendu.")
            role = message.get("role")
            if not isinstance(role, str) or not role.strip():
                raise ValueError(f"Message #{index} : 'role' (str) requis.")
            content = message.get("content")
            if isinstance(content, str):
                if not content.strip():
                    raise ValueError(f"Message #{index} : 'content' vide.")
            elif isinstance(content, list):
                if not content:
                    raise ValueError(f"Message #{index} : 'content' vide.")
                for block in content:
                    if not isinstance(block, dict) or not isinstance(
                        block.get("text", ""), str
                    ):
                        raise ValueError(
                            f"Message #{index} : bloc 'text' (str) requis."
                        )
            else:
                raise ValueError(f"Message #{index} : 'content' str|list requis.")
        return value

    def effective_messages(self) -> list[dict[str, Any]]:
        """Messages effectifs envoyes au LLM (system prefixe si present)."""
        if self.system_prompt:
            return [{"role": "system", "content": self.system_prompt}, *self.messages]
        return list(self.messages)


class SamplingResponse(BaseModel):
    """Reponse de sampling MCP (completion LLM)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str = Field(..., min_length=1)
    model: str = Field(default="")
    stop_reason: str = Field(default="end_turn", min_length=1)

    def to_dict(self) -> dict[str, Any]:
        """Projection MCP de la reponse (``createMessage`` result)."""
        result: dict[str, Any] = {
            "role": "assistant",
            "content": {"type": "text", "text": self.text},
            "stopReason": self.stop_reason,
        }
        if self.model:
            result["model"] = self.model
        return result
