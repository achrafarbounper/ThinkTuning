"""Infrastructure d'autorisation (P2 Lot A) — PDP Casbin embarquée.

Package fail-closed : une politique invalide ou une PDP indisponible ne
peut JAMAIS aboutir à une autorisation implicite. Voir ``enforcer.py``
pour la sémantique strict / legacy_permissive.
"""

from app.infrastructure.security.authz.casbin_pdp import CasbinPDP
from app.infrastructure.security.authz.enforcer import AuthzPolicyEnforcer
from app.infrastructure.security.authz.policy_document import PolicyDocument
from app.infrastructure.security.authz.tool_capabilities import ToolCapabilityRegistry

__all__ = [
    "AuthzPolicyEnforcer",
    "CasbinPDP",
    "PolicyDocument",
    "ToolCapabilityRegistry",
]
