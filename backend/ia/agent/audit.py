"""Persistent tool-call audit backed by MongoDB."""

from __future__ import annotations

import logging
from typing import Any

from app.infrastructure.persistence.mongodb import MongoToolAuditStore

logger = logging.getLogger("thinktuning.agent.audit")
RETENTION_DAYS = 30


class AuditStore(MongoToolAuditStore):
    """Compatibility name retained for callers of the historical API."""

    def __init__(self, path: str | None = None, provider=None) -> None:
        super().__init__(provider=provider)


_store: AuditStore | None = None


def get_audit_store() -> AuditStore:
    global _store
    if _store is None:
        _store = AuditStore()
    return _store


def reset_audit_store(path: str | None = None) -> None:
    global _store
    _store = AuditStore(path=path)


def log_tool_call(
    tool_name: str | None = None, args: dict[str, Any] | None = None,
    result: Any = None, duration_ms: float = 0.0, success: bool = True,
    error_message: str | None = None, job_id: str | None = None,
    *, tool: str | None = None, **kwargs: Any,
) -> str:
    actual = tool_name or tool
    if not actual:
        raise ValueError("tool_name (ou tool) est requis")
    return get_audit_store().log_tool_call(
        actual, args or {}, result, duration_ms, success, error_message,
        job_id or kwargs.get("job_id"),
    )


def get_audit_trail(job_id=None, tool_name=None, limit=100):
    return get_audit_store().get_trail(job_id=job_id, tool_name=tool_name, limit=limit)


def get_tool_history(tool_name: str, limit: int = 50) -> list[dict[str, Any]]:
    return get_audit_store().get_trail(tool_name=tool_name, limit=limit)


def clear_audit_log(job_id: str | None = None) -> None:
    q = {"job_id": job_id} if job_id else {}
    get_audit_store().c.delete_many(q)


def set_audit_db_path(path: str) -> None:
    reset_audit_store(path)


def get_audit_db_path() -> str:
    return "mongodb://configured/service_audit"
