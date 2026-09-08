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
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

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
DEFAULT_MCP_VERSION = MCPVersion(major=0, minor=1, patch=0)


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
