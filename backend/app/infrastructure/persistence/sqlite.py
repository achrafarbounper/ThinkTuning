"""Stores SQLite typés — adaptateurs strangler au-dessus du legacy.

Chaque classe hérite de l'implémentation legacy (MÊME schéma SQLite : zéro
migration de données) et documente le contrat typé du port correspondant.
Le comportement est strictement celui du legacy : ces classes n'ajoutent que
la frontière de typage qui permettra de swapper l'implémentation
(SQLAlchemy/Postgres) sans toucher au domaine.
"""

from __future__ import annotations

from typing import Any, cast

from core.approval_store import ApprovalStore as _LegacyApprovalStore
from core.audit_store import AuditStore as _LegacyAuditStore
from core.flow_store import FlowStore as _LegacyFlowStore
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
        thinking: str = "",
    ) -> dict[str, Any] | None:
        return super().append_message(
            session_id, role, content, tool_calls=tool_calls, thinking=thinking
        )


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

    def create(  # type: ignore[override]  # convention port : LIGNE, pas l'id legacy
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


class SqliteFlowStore(_LegacyFlowStore):
    """``FlowStorePort`` — journal des sessions multi-agents (Flow Map).

    Chaque session est une timeline horodatée d'événements SSE, rejouable
    dans le dashboard. L'héritage du legacy préserve le schéma SQLite
    (zéro migration de données).
    """

    def start_flow(self, prompt: str, model: str = "", source: str = "api") -> dict[str, Any]:
        return super().start_flow(prompt, model=model, source=source)

    def append_event(self, flow_id: str, event: str, data: dict, at_ms: float) -> None:
        return super().append_event(flow_id, event, data, at_ms)

    def finish_flow(
        self,
        flow_id: str,
        status: str,
        answer_summary: str = "",
        error: str | None = None,
    ) -> dict[str, Any] | None:
        return super().finish_flow(flow_id, status, answer_summary=answer_summary, error=error)

    def get(self, flow_id: str) -> dict[str, Any] | None:
        return super().get(flow_id)

    def list(self, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        return super().list(limit=limit, status=status)

    def delete(self, flow_id: str) -> bool:
        return super().delete(flow_id)


# --- Fabrique de coexistence -------------------------------------------------
# Pendant la migration, la couche applicative reçoit LES SINGLETONS legacy
# (même instance, même base SQLite) derrière le typage du port : zéro changement
# de comportement, un seul point à modifier le jour du swap d'implémentation.

from app.domain.ports import (  # noqa: E402
    ApprovalStorePort,
    AuditStorePort,
    FlowStorePort,
    RunStorePort,
    SessionStorePort,
)


def default_session_store() -> SessionStorePort:
    """Store de session par défaut (singleton legacy, même base)."""
    from core.session_store import get_session_store

    return get_session_store()


def default_audit_store() -> AuditStorePort:
    """Store d'audit par défaut (singleton legacy, même base).

    Coexistence strangler assumée (identité du singleton verrouillée par
    ``test_persistence_ports``) : le legacy satisfait le port SAUF ``query``
    (renvoie l'enveloppe ``items/total/limit/offset`` au lieu de la liste) —
    le ``cast`` documente cette dette sans changer le comportement.
    """
    from core.audit_store import get_audit_store

    return cast(AuditStorePort, get_audit_store())


def default_run_store() -> RunStorePort:
    from core.run_store import get_run_store

    return get_run_store()


def default_approval_store() -> ApprovalStorePort:
    """Store d'approbation par défaut (singleton legacy, même base).

    Coexistence strangler assumée (identité du singleton verrouillée par
    ``test_persistence_ports``) : le legacy satisfait le port SAUF ``create``
    (renvoie l'identifiant brut au lieu de la ligne) — le ``cast`` documente
    cette dette ; la façade typée de référence est
    ``app/infrastructure/legacy_approval_store``.
    """
    from core.approval_store import get_approval_store

    return cast(ApprovalStorePort, get_approval_store())


def default_flow_store() -> FlowStorePort:
    """Store de sessions multi-agents par défaut (singleton legacy, même base)."""
    from core.flow_store import get_flow_store

    return get_flow_store()
