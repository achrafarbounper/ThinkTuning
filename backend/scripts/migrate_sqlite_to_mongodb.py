"""Idempotent SQLite -> MongoDB Atlas migration.

Usage:
  python scripts/migrate_sqlite_to_mongodb.py --uri "$MONGODB_URI" --database thinktuning

SQLite files are never modified or deleted. Known application tables are
normalized into the collections consumed by the MongoDB runtime adapters;
unknown tables are retained in ``sqlite_<filename>_<table>`` archive
collections. Running the command repeatedly replaces the same keys and reports
parity counts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from pymongo import MongoClient


DEFAULT_FILES = (
    "experiments/jobs.db", "experiments/agent_sessions.db",
    "experiments/agent_runs.db", "experiments/agent_flows.db",
    "experiments/agent_approvals.db", "experiments/agent_audit.db",
    "experiments/agent_settings.db", "experiments/mcp_clients.db",
)


def export_sqlite(path: Path) -> dict[str, list[dict[str, Any]]]:
    if not path.exists():
        return {}
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]
        return {table: [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
                for table in tables}
    finally:
        conn.close()


def _json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def normalize_row(path: Path, table: str, row: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """Map one legacy row to the document shape used by runtime adapters."""
    source = path.name
    if table == "agent_sessions":
        return "agent_sessions", {**row, "_id": str(row["id"])}
    if table == "agent_session_messages":
        return "agent_session_messages", {
            **row,
            "id": str(row["id"]),
            "_order": int(row["id"]),
            "tool_calls": _json_value(row.pop("tool_calls_json", None), []),
        }
    if table == "agent_memory":
        return "agent_memory", {**row, "_id": str(row["key"])}
    if table == "agent_runs":
        return "agent_runs", {
            **row,
            "_id": str(row["id"]),
            "tools": _json_value(row.pop("tools_json", None), []),
        }
    if table == "agent_flows":
        return "agent_flows", {
            **row,
            "_id": str(row["id"]),
            "events": _json_value(row.pop("events_json", None), []),
        }
    if table == "agent_approvals":
        return "agent_approvals", {
            **row,
            "_id": str(row["id"]),
            "args": _json_value(row.pop("args_json", None), {}),
        }
    if table == "agent_audit":
        return "agent_audit", {
            **row,
            "_id": str(row["id"]),
            "detail": _json_value(row.pop("detail_json", None), {}),
        }
    if table == "agent_settings":
        return "agent_settings", {**row, "_id": str(row["key"])}
    if table == "mcp_clients":
        return "mcp_clients", {
            **row,
            "_id": str(row["client_id"]),
            "scope": _json_value(row.pop("scope_json", None), {}),
            "scope_usage": _json_value(row.pop("scope_usage_json", None), {}),
            "revoked": bool(row.get("revoked")),
        }
    if table == "jobs":
        payload = _json_value(row.pop("payload", None), {})
        return "jobs", {
            "_id": str(row["job_id"]),
            "job_id": str(row["job_id"]),
            "payload": payload,
            "updated_at": row.get("updated_at"),
        }
    if table == "train_metrics":
        return "train_metrics", {
            **row,
            "_id": f"{row['job_id']}:{row['epoch']}",
        }
    if table == "scheduled_jobs":
        payload = _json_value(row.pop("payload", None), {})
        return "scheduled_jobs", {
            "_id": str(row["schedule_id"]),
            "schedule_id": str(row["schedule_id"]),
            **payload,
            "updated_at": row.get("updated_at"),
        }
    return None


def migrate(files: list[Path], client: MongoClient, database: str) -> dict[str, Any]:
    db = client[database]
    report: dict[str, Any] = {"sources": {}, "total": 0}
    for path in files:
        tables = export_sqlite(path)
        source = path.name
        report["sources"][source] = {}
        for table, rows in tables.items():
            first = normalize_row(path, table, dict(rows[0])) if rows else None
            target = first[0] if first else None
            collection = db[target or f"sqlite_{path.stem}_{table}"]
            count = 0
            for row in rows:
                raw = json.dumps(row, sort_keys=True, default=str)
                key = hashlib.sha256(f"{path.resolve()}:{table}:{raw}".encode()).hexdigest()
                normalized = normalize_row(path, table, dict(row))
                doc = normalized[1] if normalized else dict(row)
                doc["_migration_key"] = key
                doc["_sqlite_source"] = str(path)
                identity = {"_id": doc["_id"]} if "_id" in doc else {"_migration_key": key}
                collection.replace_one(identity, doc, upsert=True)
                count += 1
            report["sources"][source][table] = {
                "sqlite": count,
                "mongodb": collection.count_documents({"_sqlite_source": str(path)}),
            }
            report["total"] += count
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uri", required=True)
    parser.add_argument("--database", default="thinktuning")
    parser.add_argument("--sqlite", action="append", dest="files",
                        help="SQLite file (repeatable); defaults to all application stores")
    args = parser.parse_args()
    files = [Path(p) for p in args.files] if args.files else [
        *[Path(p) for p in DEFAULT_FILES],
        *Path("experiments").glob("*.db"),
    ]
    files = list(dict.fromkeys(files))
    client = MongoClient(args.uri, serverSelectionTimeoutMS=5000)
    try:
        report = migrate(files, client, args.database)
        print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
