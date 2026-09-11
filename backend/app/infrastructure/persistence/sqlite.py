"""Compatibility aliases for the removed SQLite persistence layer.

The module remains importable for downstream code during the API transition,
but every alias resolves to the MongoDB implementation and no SQLite database
is opened.
"""

from __future__ import annotations

from .mongodb import (
    MongoApprovalStore,
    MongoAuditStore,
    MongoFlowStore,
    MongoRunStore,
    MongoSessionStore,
)

SqliteApprovalStore = MongoApprovalStore
SqliteAuditStore = MongoAuditStore
SqliteFlowStore = MongoFlowStore
SqliteRunStore = MongoRunStore
SqliteSessionStore = MongoSessionStore

__all__ = [
    "SqliteApprovalStore",
    "SqliteAuditStore",
    "SqliteFlowStore",
    "SqliteRunStore",
    "SqliteSessionStore",
]
