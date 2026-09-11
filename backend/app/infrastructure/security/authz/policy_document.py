"""Document de politique VERSIONNÉ — format contractuel du P2 Lot A.

Format JSON validé (schéma strict, champs inconnus interdits) :

    {
      "policy_version": 1,                    # int >= 1 — OBLIGATOIRE
      "default_tenant_mode": "legacy_permissive",
      "tenant_modes": {"default": "legacy_permissive"},
      "role_aliases": {"read_only": "viewer"},   # rôles MCP -> rôles canoniques
      "roles": {"viewer": ["tool.execute:read", ...]},
      "denies": [{"role": "*", "action": "tool.execute:unknown",
                  "resource": "*", "reason": "..."}]
    }

Règles :
    - ``policy_version`` s'incrémente à chaque évolution : chaque décision
      d'audit porte la version qui l'a produite (rollback traçable) ;
    - ``tenant_modes`` : ``legacy_permissive`` (shadow, comportement historique)
      ou ``strict`` (deny-by-default effectif). Un tenant ABSENT hérite de
      ``default_tenant_mode`` — et un mode inconnu est une erreur (fail-closed) ;
    - les patterns d'action et les rôles des règles ``denies`` supportent les
      jokers glob Unix (``*``, ``tool.execute:*``).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.authorization import TenantMode

# Chemin du document par défaut (embarqué dans le dépôt, versionné en git).
_DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "policies" / "default_policy.json"


class DenyRule(BaseModel):
    """Règle de refus explicite — prioritaire sur toute autorisation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str = Field(min_length=1, description="Rôle visé (joker ``*`` accepté).")
    action: str = Field(min_length=1, description="Pattern d'action (joker accepté).")
    resource: str = Field(default="*", min_length=1, description="Pattern de ressource.")
    reason: str = Field(default="", description="Raison auditée (humaine, actionnable).")

    def matches(self, *, role: str, action: str, resource: str) -> bool:
        """Vrai si la règle couvre la requête (matching glob sur les 3 axes)."""
        return (
            fnmatch.fnmatchcase(role, self.role)
            and fnmatch.fnmatchcase(action, self.action)
            and fnmatch.fnmatchcase(resource, self.resource)
        )


class PolicyDocument(BaseModel):
    """Politique d'autorisation typée et versionnée (source de vérité locale)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: int = Field(ge=1, description="Version du document (audit, rollback).")
    default_tenant_mode: TenantMode = TenantMode.LEGACY_PERMISSIVE
    tenant_modes: dict[str, TenantMode] = Field(
        default_factory=dict,
        description="Mode par tenant ; tenant absent -> default_tenant_mode.",
    )
    role_aliases: dict[str, str] = Field(
        default_factory=dict,
        description="Alias -> rôle canonique (ex. rôles MCP 'read_only' -> 'viewer').",
    )
    roles: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Rôle canonique -> patterns d'actions autorisées.",
    )
    denies: list[DenyRule] = Field(default_factory=list)

    @field_validator("roles")
    @classmethod
    def _validate_roles(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        for role, patterns in value.items():
            if not role.strip():
                raise ValueError("nom de rôle vide dans 'roles'")
            if not patterns:
                raise ValueError(f"rôle '{role}' sans aucun pattern d'action (fail-closed)")
            for pattern in patterns:
                if not isinstance(pattern, str) or not pattern.strip():
                    raise ValueError(f"pattern d'action invalide pour le rôle '{role}'")
        return value

    # ------------------------------------------------------------- helpers --

    def canonical_role(self, subject: str) -> str:
        """Résout un alias (ex. rôle MCP 'read_only') vers le rôle canonique.

        Un sujet inconnu N'EST PAS résolu : il est retourné tel quel — la PDP
        ne trouvera aucune règle et le déni par défaut s'appliquera (fail-closed).
        """
        return self.role_aliases.get(subject, subject)

    def tenant_mode(self, tenant: str) -> TenantMode:
        """Mode effectif d'un tenant (tenant inconnu -> mode par défaut)."""
        return self.tenant_modes.get(tenant, self.default_tenant_mode)

    def is_denied(self, *, role: str, action: str, resource: str) -> DenyRule | None:
        """Première règle de refus couvrant la requête (None sinon).

        Les denies sont prioritaires : même si une autorisation existe, le
        refus explicite gagne (effet ``priority(eft) || deny`` côté Casbin).
        """
        for rule in self.denies:
            if rule.matches(role=role, action=action, resource=resource):
                return rule
        return None

    def actions_for(self, role: str) -> tuple[str, ...]:
        """Patterns d'actions accordés à un rôle canonique (tuple vide si inconnu)."""
        return tuple(self.roles.get(role, ()))

    def fingerprint(self) -> str:
        """Empreinte SHA-256 du document (détection de dérive, audit)."""
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# ============================================================
# CHARGEMENT (source de vérité : fichier local versionné)
# ============================================================


def load_policy_document(
    source: dict[str, Any] | str | Path | None = None,
) -> PolicyDocument:
    """Charge et valide un document de politique.

    Args:
        source: dict brut, chemin de fichier JSON, ou None (défaut embarqué).

    Raises:
        ValueError: document invalide (version absente, mode inconnu, champs
            inconnus) — l'appelant DOIT fail-closer (pas de politique par
            défaut silencieuse).
    """
    if source is None:
        raw = json.loads(_DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    elif isinstance(source, (str, Path)):
        raw = json.loads(Path(source).read_text(encoding="utf-8"))
    else:
        raw = source
    if not isinstance(raw, dict):
        raise ValueError("document de politique : un objet JSON est attendu")
    if "policy_version" not in raw:
        raise ValueError("document de politique : 'policy_version' est obligatoire")
    try:
        return PolicyDocument.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError -> ValueError métier
        raise ValueError(f"document de politique invalide : {exc}") from exc

