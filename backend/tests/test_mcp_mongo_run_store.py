from __future__ import annotations

from unittest.mock import patch

import mongomock

from app.infrastructure.persistence.mcp_mongo_run_store import MongoMCPDurableRunStore
from app.infrastructure.persistence.mongodb import MongoClientProvider, MongoConfig


def _store() -> MongoMCPDurableRunStore:
    provider = MongoClientProvider(
        MongoConfig(uri="mongodb://localhost", database="test_mcp"),
        client=mongomock.MongoClient(),
    )
    return MongoMCPDurableRunStore(provider)


def test_mongo_durable_store_persists_events_and_leases() -> None:
    store = _store()
    store.create("run-1", request_fingerprint="fp")
    store.transition("run-1", "running")
    store.append_event("run-1", {"event_id": "evt-1", "event": "worker.completed"})
    store.append_event("run-1", {"event_id": "evt-1", "event": "replayed"})
    store.acquire_lease("run-1", "worker-a")
    assert store.list_events("run-1") == [
        {"event_id": "evt-1", "event": "worker.completed"}
    ]
    assert store.list_events_after("run-1", 0)[0]["sequence"] == 1
    assert store.list_events_after("run-1", 1) == []
    assert store.get("run-1").lease_owner == "worker-a"


def test_mongo_durable_store_allows_multiple_events_without_event_id() -> None:
    store = _store()
    store.create("run-1")

    store.append_event("run-1", {"event": "worker.thinking", "thinking": "first"})
    store.append_event("run-1", {"event": "worker.thinking", "thinking": "second"})

    assert store.list_events("run-1") == [
        {"event": "worker.thinking", "thinking": "first"},
        {"event": "worker.thinking", "thinking": "second"},
    ]


def test_mongo_durable_store_recovers_stale_event_sequence_counter() -> None:
    store = _store()
    store.create("run-1")
    store.append_event("run-1", {"event": "first"})
    store.runs.update_one({"_id": "run-1"}, {"$set": {"event_sequence": 0}})

    store.append_event("run-1", {"event": "second"})

    assert [event["event"] for event in store.list_events_after("run-1")] == [
        "first",
        "second",
    ]


def test_mongo_durable_store_scopes_event_id_index_to_identified_events() -> None:
    store = _store()

    index = store.events.index_information()[
        MongoMCPDurableRunStore.EVENT_ID_INDEX
    ]
    assert index["unique"] is True
    assert index["partialFilterExpression"] == {
        "event_id": {"$type": "string"}
    }


def test_mongo_durable_store_lists_and_cancels() -> None:
    store = _store()
    store.create("run-1")
    store.transition("run-1", "running")
    cancelled = store.cancel("run-1", reason="client disconnected")
    assert cancelled.state == "cancelled"
    assert store.list_runs(state="cancelled")[0].run_id == "run-1"
    assert store.list_events("run-1")[-1]["event"] == "run_cancelled"


def test_mongo_durable_store_rejects_second_active_lease() -> None:
    store = _store()
    store.create("run-1")
    store.acquire_lease("run-1", "worker-a")
    try:
        store.acquire_lease("run-1", "worker-b")
    except ValueError as exc:
        assert "leased by another owner" in str(exc)
    else:
        raise AssertionError("a second active lease must be rejected")


def test_mongo_durable_store_rejects_stale_transition() -> None:
    store = _store()
    store.create("run-1")
    snapshot = store.get("run-1")
    assert snapshot is not None
    store.runs.update_one({"_id": "run-1"}, {"$inc": {"version": 1}})

    with patch.object(store, "get", side_effect=[snapshot, snapshot]):
        try:
            store.transition("run-1", "running")
        except ValueError as exc:
            assert "modified concurrently" in str(exc)
        else:
            raise AssertionError("a stale transition must be rejected")

    current = store.get("run-1")
    assert current is not None
    assert current.state == "pending"
    assert current.version == 1


def test_mongo_durable_store_configures_event_retention_ttl() -> None:
    store = MongoMCPDurableRunStore(
        MongoClientProvider(
            MongoConfig(uri="mongodb://localhost", database="test_mcp_ttl"),
            client=mongomock.MongoClient(),
        ),
        retention_days=14,
    )

    index = store.events.index_information()[
        MongoMCPDurableRunStore.EVENT_RETENTION_INDEX
    ]
    assert index["expireAfterSeconds"] == 14 * 86400


def test_mongo_durable_store_can_disable_event_retention() -> None:
    store = MongoMCPDurableRunStore(
        MongoClientProvider(
            MongoConfig(uri="mongodb://localhost", database="test_mcp_no_ttl"),
            client=mongomock.MongoClient(),
        ),
        retention_days=0,
    )

    assert MongoMCPDurableRunStore.EVENT_RETENTION_INDEX not in (
        store.events.index_information()
    )
