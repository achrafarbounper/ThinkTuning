"""Factory de l'enforcer d'autorisation — singleton par process (P2 Lot A).

Le flag ``security_authz_casbin`` (env ``AGENT_SECURITY_AUTHZ_CASBIN``) pilote
l'activation ; l'enforcer est construit UNE fois par process (lazy, thread-safe).
Rollback = unset de la variable d'environnement (aucun état à purger : la PDP
est purement mémoire, sans store partagé).

Le tenant des runs agent est lu de l'env ``AGENT_AUTHZ_TENANT`` (défaut
``default``) : l'isolation par environnement reste opérationnelle même avant
l'arrivée des JWT multi-tenant (Lot B).
"""

from __future__ import annotations

import os
import threading

from app.domain.errors import PolicyUnavailableError
from app.infrastructure.security.authz.casbin_pdp import CasbinPDP
from app.infrastructure.security.authz.enforcer import AuthzPolicyEnforcer
from app.infrastructure.security.authz.policy_document import load_policy_document
from app.infrastructure.security.authz.tool_capabilities import ToolCapabilityRegistry

FLAG_NAME = "security_authz_casbin"
DEFAULT_TENANT = "default"

_lock = threading.Lock()
_default_enforcer: AuthzPolicyEnforcer | None = None


def authz_enabled() -> bool:
    """Flag d'activation (relecture à chaque appel, convention du projet)."""
    from core.feature_flags import flag

    return flag(FLAG_NAME)


def default_tenant() -> str:
    """Tenant des runs agent (env ``AGENT_AUTHZ_TENANT``, défaut ``default``)."""
    return os.getenv("AGENT_AUTHZ_TENANT", "").strip() or DEFAULT_TENANT


def get_default_enforcer() -> AuthzPolicyEnforcer:
    """Enforcer partagé du process (construit une fois, thread-safe).

    Raises:
        PolicyUnavailableError: politique invalide / PDP non constructible —
            l'appelant fail-close (jamais d'autorisation implicite).
    """
    global _default_enforcer
    if _default_enforcer is not None:
        return _default_enforcer
    with _lock:
        if _default_enforcer is None:
            try:
                policy = load_policy_document()
                pdp = CasbinPDP(policy)
            except Exception as exc:  # noqa: BLE001 — fail-closed volontaire
                raise PolicyUnavailableError(
                    f"construction de la PDP impossible : {exc}"
                ) from exc
            _default_enforcer = AuthzPolicyEnforcer(
                pdp=pdp,
                policy=policy,
                capabilities=ToolCapabilityRegistry(),
            )
    return _default_enforcer


def reset_default_enforcer() -> None:
    """Réinitialise le singleton (tests uniquement)."""
    global _default_enforcer
    with _lock:
        _default_enforcer = None
