"""PDP Casbin embarquée — moteur de décision par process (P2 Lot A).

Décisions d'architecture (plan P2) :

    - EMBARQUÉE : chaque process charge sa politique en mémoire au démarrage ;
      aucune PDP réseau (pas de SaaS, compatible Render Free) et aucune I/O
      par décision → p95 local < 5 ms (vérifié par test de charge) ;
    - MODÈLE RBAC+ABAC : requête ``(subject, tenant, action, resource)`` ;
      policies ``(sub, tenant, act, obj, eft)`` avec effet ``priority`` — le
      deny explicite GAGNE sur tout allow (deny-by-default structurel) ;
    - FAIL-CLOSED : toute exception Casbin → ``AuthzDecision.deny
      (fail_closed=True)`` — une PDP malade n'autorise jamais ;
    - SWAPPABLE : le port ``PolicyDecisionPoint`` (domaine) est implémenté
      ici mais un adaptateur OPA/Rego pourrait le remplacer sans toucher
      aux use cases.

Le modèle est construit PROGRAMMATIQUEMENT (pas de fichier .conf) : la
politique d'autorisation reste UN document JSON versionné, chargé par
``PolicyDocument`` — une seule source de vérité pour l'audit.
"""

from __future__ import annotations

import fnmatch
import logging
import threading
import time

import casbin

from app.domain.authorization import (
    REASON_PDP_UNAVAILABLE,
    RULE_PDP_UNAVAILABLE,
    AuthzDecision,
    AuthzRequest,
)
from app.infrastructure.security.authz.policy_document import PolicyDocument

logger = logging.getLogger("thinktuning.security.authz")

# Modèle Casbin : RBAC (g) + ABAC (tenant/action/resource en attributs), effet
# « deny-overrides par priorité » : les policies deny sont chargées AVANT les
# allow et ``priority(p.eft) || deny`` fait gagner la première matchante.
# Champs abrégés (s/t/a/o) pour rester sous 100 colonnes.
_MODEL_TEXT = """
[request_definition]
r = s, t, a, o

[policy_definition]
p = s, t, a, o, eft

[policy_effect]
e = priority(p.eft) || deny

[matchers]
m = glob(p.s, r.s) && (p.t == r.t || p.t == "*") && glob(p.a, r.a) && glob(p.o, r.o)
"""


def _glob_match(left: str, right: str) -> bool:
    """Matching glob bidirectionnel (jokers ``*``) pour le matcher Casbin.

    Casbin passe les arguments des fonctions personnalisées dans l'ordre
    d'écriture de l'expression (``glob(p.x, r.x)`` → ``(p.x, r.x)``) : on
    accepte le joker sur CHACUN des deux côtés et on tente le match dans les
    deux sens — déterministe et insensible à l'ordre (pattern, valeur).
    """
    if left == "*" or right == "*":
        return True
    return fnmatch.fnmatchcase(left, right) or fnmatch.fnmatchcase(right, left)


class CasbinPDP:
    """Adaptateur PDP : ``PolicyDocument`` -> ``PolicyDecisionPoint``."""

    def __init__(self, policy: PolicyDocument) -> None:
        self._policy = policy
        self._enforcer = self._build_enforcer(policy)
        self._lock = threading.Lock()  # Garantit la sérialisation des décisions

    # ------------------------------------------------------------- setup ----

    @staticmethod
    def _build_enforcer(policy: PolicyDocument) -> casbin.Enforcer:
        """Construit l'enforcer en mémoire depuis le document typé."""
        model = casbin.Model()
        model.load_model_from_text(_MODEL_TEXT)
        enforcer = casbin.Enforcer(model)
        enforcer.add_function("glob", _glob_match)

        # 1. denies D'ABORD (priorité) : le refus explicite gagne toujours.
        for rule in policy.denies:
            enforcer.add_policy([rule.role, "*", rule.action, rule.resource, "deny"])
        # 2. allows : rôle canonique -> patterns d'actions (resource = ``*`` :
        #    la granularité par outil passe par l'axe action ; le registre de
        #    capacités déclare les outils, la politique déclare les droits).
        for role, actions in policy.roles.items():
            for action in actions:
                enforcer.add_policy([role, "*", action, "*", "allow"])
        return enforcer

    # ------------------------------------------------------------- contrats --

    @property
    def policy_version(self) -> int:
        """Version de la politique chargée (audit, rollback)."""
        return self._policy.policy_version

    @property
    def policy_fingerprint(self) -> str:
        """Empreinte du document (détection de dérive)."""
        return self._policy.fingerprint()

    def authorize(self, request: AuthzRequest) -> AuthzDecision:
        """Décide une requête — fail-closed sur toute erreur interne."""
        role = self._policy.canonical_role(request.subject)
        # Règles de refus explicites : court-circuit déterministe, raison
        # auditable, zéro dépendance Casbin (résilience > confort).
        deny_rule = self._policy.is_denied(
            role=request.subject, action=request.action, resource=request.resource
        )
        if deny_rule is not None and not _glob_match(role, deny_rule.role):
            deny_rule = None  # le rôle canonique n'est pas visé par la règle
        started = time.perf_counter()
        try:
            with self._lock:
                allowed = self._enforcer.enforce(
                    role, request.tenant, request.action, request.resource
                )
        except Exception as exc:  # noqa: BLE001 — fail-closed volontaire
            logger.error(
                "PDP Casbin en échec (%s) : refus fail-closed pour %s", exc, request.action
            )
            return AuthzDecision.deny(
                reason=REASON_PDP_UNAVAILABLE,
                policy_version=self._policy.policy_version,
                rule=RULE_PDP_UNAVAILABLE,
                fail_closed=True,
            )
        decision_ms = round((time.perf_counter() - started) * 1000, 3)
        if allowed:
            logger.debug(
                "Décision PDP allow en %s ms (policy v%s)",
                decision_ms,
                self._policy.policy_version,
            )
            return AuthzDecision.allow(
                reason="autorisation accordée par la politique",
                policy_version=self._policy.policy_version,
                rule=f"role:{role}",
            )
        if deny_rule is not None:
            return AuthzDecision.deny(
                reason=deny_rule.reason or "refus explicite de la politique",
                policy_version=self._policy.policy_version,
                rule=f"deny:{deny_rule.action}",
            )
        return AuthzDecision.deny_default(policy_version=self._policy.policy_version)

    # ------------------------------------------------------------ introspec --

    def policies(self) -> list[list[str]]:
        """Policies chargées (introspection / tests / diagnostics)."""
        return [list(p) for p in self._enforcer.get_policy()]


def build_pdp(policy: PolicyDocument) -> CasbinPDP:
    """Construit une PDP validée (lève si le document est invalide)."""
    return CasbinPDP(policy)

