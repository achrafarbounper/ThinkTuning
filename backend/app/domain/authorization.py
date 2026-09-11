"""Autorisation agentique du domaine (P2 Lot A) — modèle PUR, sans Casbin.

Ce module porte les **valeur objets** de l'autorisation : la requête (qui
demande quoi sur quelle ressource) et la décision (autorisé / refusé, pourquoi,
selon quelle version de politique). Il est volontairement SANS dépendance vers
l'infrastructure : Casbin, Valkey ou un futur PDP distant (OPA) sont des
adaptateurs du port ``PolicyDecisionPoint`` (cf. app/domain/ports).

Alignement avec l'existant :

    - ``app/domain/entities/plan.py`` : ``ActionCategory`` (catégorie de risque
      d'un outil) et ``Decision`` (auto_approve / approve / reject) — la PDP
      dit « le sujet a-t-il le droit » ; la sandbox dit « faut-il une
      validation humaine ». Les deux couches restent indépendantes ;
    - ``app/infrastructure/mcp/security/scope_enforcer.py`` : rôles MCP
      (``read_only`` / ``contributor`` / ``operator`` / ``admin``) — mappés sur
      les rôles canoniques via ``role_aliases`` de la politique (aucune
      duplication).

Règles d'or (fail-closed, « deny-by-default ») :

    1. toute action NON déclarée est refusée en mode ``strict`` ;
    2. une PDP indisponible ne JAMAIS autoriser (déni explicite, audité) ;
    3. les décisions portent ``policy_version`` : l'audit sait quelle politique
       a jugé, et le rollback de politique est traçable.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.domain.entities.plan import ActionCategory

# ============================================================
# ÉNUMÉRATIONS
# ============================================================


class TenantMode(StrEnum):
    """Mode d'autorisation d'un tenant.

    LEGACY_PERMISSIVE :
        comportement historique conservé (verdicts ``sandbox_policy``),
        la PDP est évaluée en *shadow* : décision enregistrée / auditée mais
        non contraignante. Zéro changement de comportement — rollout sûr.
    STRICT :
        deny-by-default effectif : seule une règle explicite de la politique
        autorise l'action ; toute règle absente ou ambiguë → refus.
    """

    LEGACY_PERMISSIVE = "legacy_permissive"
    STRICT = "strict"

    @property
    def enforcing(self) -> bool:
        """Vrai si la décision PDP est contraignante (et pas seulement auditée)."""
        return self is TenantMode.STRICT


class AuthzEffect(StrEnum):
    """Effet d'une décision de la PDP (modèle Casbin ``p.eft``)."""

    ALLOW = "allow"
    DENY = "deny"


# ============================================================
# VALEUR OBJETS
# ============================================================


class _FrozenModel(BaseModel):
    """Base commune : immuable, champs inconnus interdits (fail-fast)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class AuthzRequest(_FrozenModel):
    """Requête d'autorisation normalisée (sujet → action → ressource).

    Modèle de décision compatible Casbin RBAC+ABAC :
        - ``subject``  : rôle canonique du demandeur (viewer/operator/admin/
          agent/service — jamais un identifiant utilisateur brut) ;
        - ``tenant``   : périmètre d'isolation (``default``, ``staging``...) ;
        - ``action``   : verbe normalisé, ex. ``tool.execute:write`` ;
        - ``resource`` : objet visé, ex. le nom de l'outil (``write_file``).

    ``context`` porte les attributs ABAC non structurants (hash d'arguments,
    catégorie de risque, source d'appel) : JAMAIS de valeur métier ni de
    secret — l'audit d'autorisation est anonymisé par construction.
    """

    subject: str = Field(min_length=1, description="Rôle canonique du demandeur.")
    tenant: str = Field(default="default", min_length=1, description="Tenant / environnement.")
    action: str = Field(min_length=1, description="Verbe normalisé (ex. tool.execute:write).")
    resource: str = Field(min_length=1, description="Ressource visée (ex. nom d'outil).")
    context: dict[str, Any] = Field(
        default_factory=dict,
        description="Attributs ABAC (args_hash, category, source) — jamais de valeur secrète.",
    )

    @classmethod
    def for_tool(
        cls,
        *,
        tool: str,
        category: ActionCategory | str,
        subject: str,
        tenant: str = "default",
        args_hash: str = "",
        source: str = "agent",
    ) -> AuthzRequest:
        """Construit la requête d'autorisation d'un appel d'outil.

        L'action est dérivée de la catégorie de risque déclarée : une catégorie
        inconnue (outil non déclaré) produit ``tool.execute:unknown`` — la
        requête la plus restrictive possible (deny-by-default).
        """
        cat = category.value if isinstance(category, ActionCategory) else str(category)
        return cls(
            subject=subject,
            tenant=tenant,
            action=f"tool.execute:{cat}",
            resource=tool,
            context={"args_hash": args_hash, "category": cat, "source": source},
        )


class AuthzDecision(_FrozenModel):
    """Décision d'autorisation auditable et traçable.

    Attributs :
        allowed:        l'action est-elle permise (False = refus, fail-closed) ;
        effect:         effet brut de la PDP (allow / deny) ;
        reason:         raison humaine actionnable (journal + feedback LLM) ;
        policy_version: version de la politique qui a jugé (traçabilité) ;
        rule:           identifiant de la règle décisive (audit fin) ;
        enforced:       la décision est-elle contraignante (strict) ou en
                        shadow (legacy_permissive : auditée, non appliquée) ;
        fail_closed:    True si le refus provient d'une indisponibilité PDP
                        (et non d'une règle explicite).
    """

    allowed: bool
    effect: AuthzEffect = AuthzEffect.DENY
    reason: str = ""
    policy_version: int = 0
    rule: str = ""
    enforced: bool = False
    fail_closed: bool = False

    @classmethod
    def allow(
        cls,
        *,
        reason: str,
        policy_version: int,
        rule: str = "",
        enforced: bool = True,
    ) -> AuthzDecision:
        """Décision positive explicite (jamais par défaut)."""
        return cls(
            allowed=True,
            effect=AuthzEffect.ALLOW,
            reason=reason,
            policy_version=policy_version,
            rule=rule,
            enforced=enforced,
        )

    @classmethod
    def deny(
        cls,
        *,
        reason: str,
        policy_version: int,
        rule: str = "",
        enforced: bool = True,
        fail_closed: bool = False,
    ) -> AuthzDecision:
        """Décision négative (règle explicite OU fail-closed PDP)."""
        return cls(
            allowed=False,
            effect=AuthzEffect.DENY,
            reason=reason,
            policy_version=policy_version,
            rule=rule,
            enforced=enforced,
            fail_closed=fail_closed,
        )

    @classmethod
    def deny_default(cls, *, policy_version: int, enforced: bool = True) -> AuthzDecision:
        """Refus par défaut : AUCUNE règle ne couvre la requête (deny-by-default)."""
        return cls.deny(
            reason="aucune règle de politique ne couvre cette action (deny-by-default)",
            policy_version=policy_version,
            rule="default_deny",
            enforced=enforced,
        )


# ============================================================
# CONSTANTES D'AUDIT (raisons stables, comparables en tests)
# ============================================================

REASON_PDP_UNAVAILABLE = "pdp indisponible — refus fail-closed"
RULE_PDP_UNAVAILABLE = "pdp_unavailable"
