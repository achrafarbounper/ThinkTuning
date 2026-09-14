from __future__ import annotations

import mongomock

from app.infrastructure.persistence.mcp_mongo_run_store import MongoMCPDurableRunStore
from app.infrastructure.persistence.mcp_run_store import MCPDurableRunStore
from app.infrastructure.persistence.mcp_sqlite_migration import migrate_sqlite_to_mongo
from app.infrastructure.persistence.mongodb import MongoClientProvider, MongoConfig


def _mongo_store() -> MongoMCPDurableRunStore:
    return MongoMCPDurableRunStore(
        MongoClientProvider(
            MongoConfig(uri="mongodb://localhost", database="migration"),
            client=mongomock.MongoClient(),
        ),
        retention_days=0,
    )


def test_sqlite_migration_preserves_state_and_event_sequences(tmp_path) -> None:
    sqlite_store = MCPDurableRunStore(tmp_path / "runs.db")
    sqlite_store.create("run-1", request_fingerprint="fp")
    sqlite_store.transition("run-1", "running")
    sqlite_store.append_event("run-1", {"event_id": "e1", "event": "start"})
    sqlite_store.append_event("run-1", {"event_id": "e2", "event": "done"})

    mongo_store = _mongo_store()
    result = migrate_sqlite_to_mongo(sqlite_store.path, mongo_store)

    assert result == {
        "runs_seen": 1,
        "runs_migrated": 1,
        "runs_skipped": 0,
        "events_migrated": 2,
    }
    assert mongo_store.get("run-1").request_fingerprint == "fp"
    assert [event["sequence"] for event in mongo_store.list_events_after("run-1")] == [1, 2]


def test_sqlite_migration_is_idempotent_and_supports_dry_run(tmp_path) -> None:
    sqlite_store = MCPDurableRunStore(tmp_path / "runs.db")
    sqlite_store.create("run-1")
    mongo_store = _mongo_store()

    preview = migrate_sqlite_to_mongo(sqlite_store.path, mongo_store, dry_run=True)
    assert preview["runs_migrated"] == 1
    assert mongo_store.get("run-1") is None

    migrate_sqlite_to_mongo(sqlite_store.path, mongo_store)
    retry = migrate_sqlite_to_mongo(sqlite_store.path, mongo_store)
    assert retry["runs_skipped"] == 1
    assert retry["runs_migrated"] == 0
