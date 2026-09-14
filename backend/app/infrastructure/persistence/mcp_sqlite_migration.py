"""Idempotent migration of legacy SQLite durable MCP runs to MongoDB."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.infrastructure.persistence.mcp_mongo_run_store import MongoMCPDurableRunStore


def migrate_sqlite_to_mongo(
    sqlite_path: str | Path,
    mongo_store: MongoMCPDurableRunStore,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Copy runs/events while preserving IDs and event sequences.

    Existing Mongo documents are skipped, making retries safe. The source
    SQLite database is never modified.
    """
    counts = {"runs_seen": 0, "runs_migrated": 0, "runs_skipped": 0, "events_migrated": 0}
    with sqlite3.connect(str(sqlite_path)) as connection:
        runs = connection.execute(
            "SELECT run_id, state_json FROM mcp_durable_runs ORDER BY run_id"
        ).fetchall()
        for run_id, state_json in runs:
            counts["runs_seen"] += 1
            run_exists = mongo_store.runs.find_one({"_id": str(run_id)}) is not None
            if run_exists:
                counts["runs_skipped"] += 1
            snapshot = json.loads(state_json)
            events = connection.execute(
                """
                SELECT sequence, event_id, event_json
                FROM mcp_durable_events
                WHERE run_id = ? ORDER BY sequence ASC
                """,
                (str(run_id),),
            ).fetchall()
            if dry_run:
                if not run_exists:
                    counts["runs_migrated"] += 1
                    counts["events_migrated"] += len(events)
                continue

            document: dict[str, Any] = {
                "_id": str(run_id),
                **snapshot,
                "event_sequence": max((int(row[0]) for row in events), default=0),
            }
            _normalize_dates(document)
            if not run_exists:
                mongo_store.runs.insert_one(document)
                counts["runs_migrated"] += 1
            for sequence, event_id, event_json in events:
                mongo_store.events.update_one(
                    {"run_id": str(run_id), "sequence": int(sequence)},
                    {
                        "$setOnInsert": {
                            "_id": f"sqlite-{run_id}-{sequence}",
                            "run_id": str(run_id),
                            "sequence": int(sequence),
                            "event_id": event_id,
                            "event": json.loads(event_json),
                            "created_at": datetime.now(UTC),
                        }
                    },
                    upsert=True,
                )
                counts["events_migrated"] += 1
    return counts


def _normalize_dates(document: dict[str, Any]) -> None:
    for key in ("created_at", "updated_at", "lease_expires_at"):
        value = document.get(key)
        if isinstance(value, str):
            document[key] = datetime.fromisoformat(value)
