# project/app/infrastructure/mcp/mcp_audit.py
"""Audit MCP — journalisation de chaque appel dans ``core/audit_store`` (S4, tâche 12).

Contrat (docs/mcp/MCP_SECURITY.md) : chaque appel MCP est tracé dans la MÊME
table ``agent_audit`` que l'agent, avec ``subject`` = ``client_id`` :

    audit_mcp_call(ACT_MCP_TOOL_CALL, subject=client_id,
                   detail={"tool": ..., "is_error": ...}, run_id=request_id)

Garanties :
    - NON BLOQUANT : un échec d'écriture d'audit ne fait JAMAIS tomber un
      appel MCP — l'incident est loggé (la disponibilité prime, l'alerte
      monitorera l'absence d'audit) ;
    - ``actor="mcp"`` distingue les entrées produites par la surface MCP de
      celles des routes agent (``actor`` par défaut ``system``) ;
    - interrupteur ``MCP_AUDIT_ENABLED`` (défaut ``true``) — les déploiements
      qui désactivent l'audit le font explicitement, jamais par accident.

L'import de ``core.audit_store`` est paresseux (et léger : stdlib) : importer
ce module ne crée AUCUNE base SQLite (le store est résolu à l'appel).
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("thinktuning.mcp.audit")

# Interrupteur global : ``0``/``false``/``off``/``no`` désactive l'écriture.
_MCP_AUDIT_ENABLED = os.getenv("MCP_AUDIT_ENABLED", "true").strip().lower() not in {
    "0",
    "false",
    "no",
    "off",
}


def mcp_audit_enabled() -> bool:
    """L'audit MCP est-il activé ? (interrupteur de rollback)."""
    return _MCP_AUDIT_ENABLED


def audit_mcp_call(
    action: str,
    *,
    subject: str = "",
    detail: dict[str, Any] | None = None,
    run_id: str | None = None,
    **_: Any,
) -> dict | None:
    """Journalise un appel MCP dans ``core/audit_store`` (non bloquant).

    Args :
        action :  action normalisée (``ACT_MCP_*`` de ``core/audit_store``) ;
        subject : ``client_id`` MCP de l'appelant (ou ``anonymous``) ;
        detail :  description de l'appel (tool/URI/prompt, arguments,
                  ``is_error``, scope) — anonymisée par le store (``redact``) ;
        run_id :  ``mcp_request_id`` (id JSON-RPC de la requête, en str).

    Returns :
        La ligne d'audit créée (``dict``) ou ``None`` si audit désactivé ou
        échoué — le serveur MCP ignore toujours la valeur de retour.
    """
    if not mcp_audit_enabled():
        return None
    try:
        from core.audit_store import get_audit_store

        return get_audit_store().log(
            action,
            subject=subject,
            detail=detail,
            run_id=run_id,
            actor="mcp",
        )
    except Exception:  # pragma: no cover - défensif, ne jamais propager
        logger.exception("Audit MCP impossible (%s) — non bloquant", action)
        return None


__all__ = ["audit_mcp_call", "mcp_audit_enabled"]
