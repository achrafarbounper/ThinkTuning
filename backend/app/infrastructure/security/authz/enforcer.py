"""Enforcer d'autorisation — PDP + modes de tenant + audit (P2 Lot A).

Sémantique (décisions du plan P2) :

    ``strict`` (deny-by-default effectif)
        la décision PDP fait LOI : ``allow`` → l'action passe à la sandbox
        (qui décide auto-approve / validation humaine) ; ``deny`` → rejet
        immédiat, audité, JAMAIS exécuté.

    ``legacy_permissive`` (shadow mode, défaut du rollout)
        la décision PDP est évaluée et AUDITÉE mais non contraignante : le
        verdict exécuté reste celui de ``sandbox_policy`` (comportement
        historique, zéro rupture). Le delta PDP/sandbox est loggué : c'est
        la lecture d'impact avant le passage en strict.

Règles transverses :
    - fail-closed : PDP en échec → ``fail_closed=True`` et rejet en strict ;
    - audit unifié : événement ``agent.authz_decision`` sur l'event bus, avec
      ``args_hash`` (jamais les valeurs) — conforme à l'audit anonymisé ;
    - tenant inconnu → mode par défaut du document (fail-closed si le défaut
      est strict ; sinon shadow — l'audit trace toujours le mode effectif).
"""

from __future__ import annotations

import logging
from typing import Any

from app.domain.authorization import AuthzDecision, AuthzRequest, TenantMode
from app.domain.entities.plan import Action, ActionCategory
from app.domain.errors import PolicyUnavailableError
from app.domain.ports import EventBusPort
from app.domain.ports.authorization_ports import PolicyDecisionPoint
from app.infrastructure.security.authz.policy_document import PolicyDocument
from app.infrastructure.security.authz.tool_capabilities import ToolCapabilityRegistry

logger = logging.getLogger("thinktuning.security.authz")


class AuthzPolicyEnforcer:
    """Facade d'autorisation : capability registry + PDP + modes de tenant.

    Un seul point d'entrée pour REST / MCP / registre / boucle agent :
    ``authorize_tool()`` — la granularité par surface se fait par le sujet
    (rôle) et le tenant, pas par du code spécifique.
    """

    def __init__(
        self,
        *,
        pdp: PolicyDecisionPoint,
        policy: PolicyDocument,
        capabilities: ToolCapabilityRegistry | None = None,
        event_bus: EventBusPort | None = None,
    ) -> None:
        if pdp is None or policy is None:  # garde-fou constructeur (fail-fast)
            raise PolicyUnavailableError("PDP ou politique d'autorisation absente")
        self._pdp = pdp
        self._policy = policy
        self._capabilities = capabilities or ToolCapabilityRegistry()
        self._event_bus = event_bus

    # ------------------------------------------------------------- propriétés --

    @property
    def policy_version(self) -> int:
        """Version de la politique active (audit / diagnostics)."""
        return self._pdp.policy_version

    @property
    def capabilities(self) -> ToolCapabilityRegistry:
        """Registre de capacités (introspection des outils déclarés)."""
        return self._capabilities

    def tenant_mode(self, tenant: str) -> TenantMode:
        """Mode effectif du tenant (document -> strict / legacy_permissive)."""
        return self._policy.tenant_mode(tenant)

    # ------------------------------------------------------------- décision --

    def authorize_tool(
        self,
        *,
        tool: str,
        subject: str,
        tenant: str = "default",
        category: ActionCategory | str | None = None,
        args_hash: str = "",
        source: str = "agent",
    ) -> AuthzDecision:
        """Autorise (ou non) un appel d'outil — point d'entrée unique.

        La catégorie est résolue via le registre de capacités si non fournie.
        La décision est auditée (event bus) ; ``enforced`` reflète le mode
        effectif du tenant (strict : contraignante, legacy : shadow).
        """
        if category is None:
            category = self._capabilities.category_for(tool)
        request = AuthzRequest.for_tool(
            tool=tool,
            category=category,
            subject=subject,
            tenant=tenant,
            args_hash=args_hash,
            source=source,
        )
        mode = self.tenant_mode(tenant)
        decision = self._pdp.authorize(request)
        enforced_decision = decision.model_copy(update={"enforced": mode.enforcing})
        self._audit(request, enforced_decision, mode)
        return enforced_decision

    def gate_for_run(
        self,
        *,
        subject: str,
        tenant: str = "default",
        source: str = "agent",
    ) -> RunAuthzGate:
        """Prépare un évaluateur léger pour la boucle agent (par run).

        Le couple (sujet, tenant) est fixé au début du run : la PDP est
        interrogée par action, sans re-résoudre le contexte à chaque étape.
        """
        return RunAuthzGate(
            enforcer=self,
            subject=self._policy.canonical_role(subject),
            tenant=tenant,
            source=source,
        )

    # ------------------------------------------------------------- audit -----

    def _audit(
        self,
        request: AuthzRequest,
        decision: AuthzDecision,
        mode: TenantMode,
    ) -> None:
        """Journal d'audit unifié (event bus + log) — sans valeur d'argument."""
        payload: dict[str, Any] = {
            "tool": request.resource,
            "subject": request.subject,
            "tenant": request.tenant,
            "action": request.action,
            "category": request.context.get("category", ""),
            "args_hash": request.context.get("args_hash", ""),
            "source": request.context.get("source", ""),
            "allowed": decision.allowed,
            "fail_closed": decision.fail_closed,
            "enforced": decision.enforced,
            "mode": mode.value,
            "policy_version": decision.policy_version,
            "rule": decision.rule,
            "reason": decision.reason,
        }
        if self._event_bus is not None:
            try:
                self._event_bus.emit("agent.authz_decision", **payload)
            except Exception:  # pragma: no cover — l'audit ne casse jamais le flux
                logger.exception("Émission de l'événement d'audit authz en échec")
        level = logging.WARNING if (decision.enforced and not decision.allowed) else logging.INFO
        logger.log(
            level,
            "authz %s tool=%s subject=%s tenant=%s mode=%s v%s rule=%s",
            "ALLOW" if decision.allowed else "DENY",
            request.resource,
            request.subject,
            request.tenant,
            mode.value,
            decision.policy_version,
            decision.rule,
        )


class RunAuthzGate:
    """Évaluateur par run : sujet/tenant fixes, décision par action.

    Retourne ``None`` quand la décision PDP n'est pas contraignante
    (shadow legacy OU allow en strict) — la boucle agent garde alors son
    verdict ``sandbox_policy`` historique. Retourne l'``AuthzDecision`` de
    refus quand le mode strict interdit l'action.
    """

    def __init__(
        self,
        *,
        enforcer: AuthzPolicyEnforcer,
        subject: str,
        tenant: str,
        source: str,
    ) -> None:
        self._enforcer = enforcer
        self._subject = subject
        self._tenant = tenant
        self._source = source
        self._mode = enforcer.tenant_mode(tenant)

    @property
    def mode(self) -> TenantMode:
        """Mode effectif du tenant pour ce run."""
        return self._mode

    def check(self, action: Action) -> AuthzDecision | None:
        """Décision PDP pour une action (None = laisser la sandbox décider)."""
        category = self._enforcer.capabilities.category_for(action.tool)
        decision = self._enforcer.authorize_tool(
            tool=action.tool,
            subject=self._subject,
            tenant=self._tenant,
            category=category,
            args_hash=action.fingerprint(),
            source=self._source,
        )
        if decision.allowed or not decision.enforced:
            return None  # allow (strict) ou shadow (legacy) : sandbox décide
        return decision
