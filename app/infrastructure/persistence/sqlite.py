"""Stores SQLite typés — adaptateurs strangler au-dessus du legacy.

Chaque classe hérite de l'implémentation legacy (MÊME schéma SQLite : zéro
migration de données) et documente le contrat typé du port correspondant.
Le comportement est strictement celui du legacy : ces classes n'ajoutent que
la frontière de typage qui permettra de swapper l'implémentation
(SQLAlchemy/Postgres) sans toucher au domaine.
"""

from __future__ import annotations

from typing import Any

from core.approval_store import ApprovalStore as _LegacyApprovalStore
from core.audit_store import AuditStore as _LegacyAuditStore
from core.run_store import RunStore as _LegacyRunStore
from core.session_store import SessionStore as _LegacySessionStore


class SqliteSessionStore(_LegacySessionStore):
    """``SessionStorePort`` — messages de session + mémoire long terme."""

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        return super().append_message(session_id, role, content, tool_calls=tool_calls)


class SqliteAuditStore(_LegacyAuditStore):
    """``AuditStorePort`` — journal d'audit (détail anonymisé à l'écriture)."""


class SqliteRunStore(_LegacyRunStore):
    """``RunStorePort`` — traçabilité des runs agent (prompt, statut, outils).

    NB : écrase ``append_tool_event`` pour rétablir la signature exacte du
    port — sans quoi l'héritage expose la méthode homonyme du module
    ``sqlite3.Connection`` (``append_tool_event(sql, parameters)``).
    """

    def append_tool_event(self, run_id: str, event: dict[str, Any]) -> None:
        return super().append_tool_event(run_id, event)

    def start_run(
        self, prompt: str, model: str = "", source: str = "api"
    ) -> dict[str, Any]:
        return super().start_run(prompt, model=model, source=source)

    def finish_run(
        self,
        run_id: str,
        status: str,
        answer_summary: str = "",
        error: str | None = None,
    ) -> dict[str, Any] | None:
        return super().finish_run(
            run_id, status, answer_summary=answer_summary, error=error
        )

    def list(
        self,
        limit: int = 50,
        status: str | None = None,
        tool: str | None = None,
    ) -> list[dict[str, Any]]:
        return super().list(limit=limit, status=status, tool=tool)


class SqliteApprovalStore(_LegacyApprovalStore):
    """``ApprovalStorePort`` — file d'approbation humaine.

    Convention ``create`` : renvoie la LIGNE créée (dict avec ``request_id``)
    et non l'identifiant brut du legacy — les use-cases s'appuient sur
    ``record.get("request_id")`` (aligné sur l'adaptateur de référence
    ``app/infrastructure/legacy_approval_store``).
    """

    def create(
        self,
        tool: str,
        args: Any,
        category: str,
        decision: str,
        reason: str,
        prompt: str = "",
        args_hash: str = "",
        status: str = "pending",
    ) -> dict[str, Any]:
        request_id = super().create(
            tool, args, category, decision, reason,
            prompt=prompt, args_hash=args_hash, status=status,
        )
        return self.get(request_id) or {"request_id": request_id, "id": request_id}


# --- Fabrique de coexistence -------------------------------------------------
# Pendant la migration, la couche applicative reçoit LES SINGLETONS legacy
# (même instance, même base SQLite) derrière le typage du port : zéro changement
# de comportement, un seul point à modifier le jour du swap d'implémentation.

from app.domain.ports import (  # noqa: E402
    ApprovalStorePort,
    AuditStorePort,
    RunStorePort,
    SessionStorePort,
)


def default_session_store() -> SessionStorePort:
    """Store de session par défaut (singleton legacy, même base)."""
    from core.session_store import get_session_store

    return get_session_store()


def default_audit_store() -> AuditStorePort:
    from core.audit_store import get_audit_store

    return get_audit_store()


def default_run_store() -> RunStorePort:
    from core.run_store import get_run_store

    return get_run_store()


def default_approval_store() -> ApprovalStorePort:
    from core.approval_store import get_approval_store

    return get_approval_store()
