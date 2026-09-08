# project/app/infrastructure/mcp/security/__init__.py
"""Sécurité MCP (S4, tâche 11) — scopes, quotas et rate limiting.

- ``scope_enforcer``    : enforceur composé (``check_scope`` / ``check_quota`` /
  ``check_rate_limit``) + les 4 catalogues par rôle (read_only 12 < contributor
  25 < operator 35 < admin 40) — docs/mcp/MCP_SECURITY.md ;
- ``rate_limit_bucket`` : primitive ``TokenBucket`` PARTAGÉE avec le middleware
  REST ``api/middlewares/rate_limit.py`` — une seule implémentation, deux
  consommateurs (clé IP côté REST, clé ``client_id`` côté MCP).

Ce paquet n'importe JAMAIS ``api`` (le stack HTTP/ML) : la suite MCP reste
légère et l'enforceur est testable isolément.
"""

from app.infrastructure.mcp.security.rate_limit_bucket import TokenBucket
from app.infrastructure.mcp.security.scope_enforcer import (
    ADMIN_ROLE_TOOLS,
    CONTRIBUTOR_ROLE_TOOLS,
    OPERATOR_ROLE_TOOLS,
    READ_ONLY_ROLE_TOOLS,
    ROLE_TOOLS,
    MCPAccessDeniedError,
    MCPEnforcerError,
    MCPQuotaExceededError,
    MCPRateLimitExceededError,
    MCPScopeEnforcer,
    check_quota,
    check_rate_limit,
    check_scope,
    default_scope_resolver,
    effective_tools,
    enforce,
    get_default_enforcer,
    is_destructive_tool,
    reset_default_enforcer,
    resolve_scope,
)

__all__ = [
    "ADMIN_ROLE_TOOLS",
    "CONTRIBUTOR_ROLE_TOOLS",
    "MCPAccessDeniedError",
    "MCPEnforcerError",
    "MCPQuotaExceededError",
    "MCPRateLimitExceededError",
    "MCPScopeEnforcer",
    "OPERATOR_ROLE_TOOLS",
    "READ_ONLY_ROLE_TOOLS",
    "ROLE_TOOLS",
    "TokenBucket",
    "check_quota",
    "check_rate_limit",
    "check_scope",
    "default_scope_resolver",
    "effective_tools",
    "enforce",
    "get_default_enforcer",
    "is_destructive_tool",
    "reset_default_enforcer",
    "resolve_scope",
]
