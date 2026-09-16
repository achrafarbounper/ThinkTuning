"""SQLite persistence for the MCP durable-run lifecycle."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.domain.ports.mcp_ports import MCPDurableRunState


class MCPDurableRunStore:
    """Thread-safe store that persists lifecycle snapshots atomically."""

    def __init__(self, path: str | Path = "experiments/mcp_runs.db") -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_durable_runs (
                    run_id TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_durable_events (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence),
                    FOREIGN KEY (run_id) REFERENCES mcp_durable_runs(run_id)
                )
                """
            )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(mcp_durable_events)").fetchall()
            }
            if "event_id" not in columns:
                connection.execute("ALTER TABLE mcp_durable_events ADD COLUMN event_id TEXT")
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS
                idx_mcp_durable_events_event_id
                ON mcp_durable_events(run_id, event_id)
                WHERE event_id IS NOT NULL
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30.0)

    def create(
        self,
        run_id: str,
        *,
        request_fingerprint: str | None = None,
    ) -> MCPDurableRunState:
        normalized_id = str(run_id or "").strip()
        if not normalized_id:
            raise ValueError("run_id must not be empty")
        state = MCPDurableRunState(
            run_id=normalized_id,
            request_fingerprint=request_fingerprint,
        )
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT 1 FROM mcp_durable_runs WHERE run_id = ?", (normalized_id,)
            ).fetchone()
            if existing is not None:
                raise ValueError(f"run {normalized_id!r} already exists")
            connection.execute(
                "INSERT INTO mcp_durable_runs(run_id, state_json) VALUES (?, ?)",
                (normalized_id, json.dumps(state.as_snapshot())),
            )
        return state

    def get(self, run_id: str) -> MCPDurableRunState | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM mcp_durable_runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        from datetime import datetime

        return MCPDurableRunState(
            run_id=payload["run_id"],
            request_fingerprint=payload.get("request_fingerprint"),
            lease_owner=payload.get("lease_owner"),
            lease_expires_at=(
                datetime.fromisoformat(payload["lease_expires_at"])
                if payload.get("lease_expires_at")
                else None
            ),
            state=payload["state"],
            phase=payload["phase"],
            checkpoint=payload["checkpoint"],
            failure_phase=payload["failure_phase"],
            worker_errors=tuple(payload["worker_errors"]),
            retry_count=payload["retry_count"],
            version=payload.get("version", 0),
            last_sequence=int(payload.get("last_sequence", 0) or 0),
            last_error=payload["last_error"],
            created_at=datetime.fromisoformat(payload["created_at"]),
            updated_at=datetime.fromisoformat(payload["updated_at"]),
        )

    def transition(self, run_id: str, new_state: str, **kwargs: Any) -> MCPDurableRunState:
        with self._lock:
            current = self.get(run_id)
            if current is None:
                raise KeyError(f"unknown MCP run {run_id!r}")
            updated = current.transition(new_state, **kwargs)
            with self._connect() as connection:
                connection.execute(
                    "UPDATE mcp_durable_runs SET state_json = ? WHERE run_id = ?",
                    (json.dumps(updated.as_snapshot()), str(run_id)),
                )
            return updated

    def append_event(self, run_id: str, event: dict[str, Any]) -> int:
        """Persiste un événement et retourne sa séquence (monotone).

        L1 (SCRUM-152) : la séquence est MÉMORISÉE sur le run
        (``last_sequence``) dans la même transaction — un client rejoué reprend
        avec ``after_sequence=last_sequence`` sans rejouer l'historique.
        """
        if self.get(run_id) is None:
            raise KeyError(f"unknown MCP run {run_id!r}")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM mcp_durable_events WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            sequence = int(row[0]) + 1
            event_id = event.get("event_id")
            if event_id is not None:
                existing = connection.execute(
                    """
                    SELECT sequence FROM mcp_durable_events
                    WHERE run_id = ? AND event_id = ?
                    """,
                    (str(run_id), str(event_id)),
                ).fetchone()
                if existing is not None:
                    # Idempotence : la séquence EXISTANTE reste le curseur.
                    return int(existing[0])
            connection.execute(
                """
                INSERT INTO mcp_durable_events(run_id, sequence, event_id, event_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    str(run_id),
                    sequence,
                    str(event_id) if event_id is not None else None,
                    json.dumps(dict(event), ensure_ascii=False),
                ),
            )
            connection.execute(
                """
                UPDATE mcp_durable_runs
                SET state_json = json_set(
                    state_json,
                    '$.last_sequence',
                    MAX(
                        COALESCE(json_extract(state_json, '$.last_sequence'), 0),
                        ?
                    )
                )
                WHERE run_id = ?
                """,
                (sequence, str(run_id)),
            )
        return sequence

    def last_sequence(self, run_id: str) -> int:
        """Dernière séquence d'événement du run (0 si aucun événement)."""
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM mcp_durable_events WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT event_json FROM mcp_durable_events
                WHERE run_id = ? ORDER BY sequence ASC
                """,
                (str(run_id),),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def list_events_after(self, run_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        if int(after_sequence) < 0:
            raise ValueError("after_sequence must be >= 0")
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT sequence, event_json FROM mcp_durable_events
                WHERE run_id = ? AND sequence > ? ORDER BY sequence ASC
                """,
                (str(run_id), int(after_sequence)),
            ).fetchall()
        return [{"sequence": int(row[0]), **json.loads(row[1])} for row in rows]

    def cancel(self, run_id: str, *, reason: str | None = None) -> MCPDurableRunState:
        state = self.transition(
            run_id,
            "cancelled",
            last_error=reason or "cancelled by request",
        )
        self.append_event(
            run_id,
            {
                "event_id": f"cancelled-{state.updated_at.isoformat()}",
                "event": "run_cancelled",
                "phase": state.phase,
                "reason": reason or "cancelled by request",
            },
        )
        return state

    def acquire_lease(
        self,
        run_id: str,
        owner: str,
        *,
        ttl_seconds: int = 60,
    ) -> MCPDurableRunState:
        normalized_owner = str(owner or "").strip()
        if not normalized_owner:
            raise ValueError("lease owner must not be empty")
        if ttl_seconds < 1:
            raise ValueError("lease ttl_seconds must be >= 1")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM mcp_durable_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown MCP run {run_id!r}")
            state = self._state_from_payload(json.loads(row[0]))
            now = datetime.now(UTC)
            if (
                state.lease_owner
                and state.lease_owner != normalized_owner
                and state.lease_expires_at
                and state.lease_expires_at > now
            ):
                raise ValueError(f"MCP run {run_id!r} is leased by another owner")
            leased = MCPDurableRunState(
                **{
                    **state.__dict__,
                    "lease_owner": normalized_owner,
                    "lease_expires_at": now + timedelta(seconds=ttl_seconds),
                    "updated_at": now,
                }
            )
            connection.execute(
                "UPDATE mcp_durable_runs SET state_json = ? WHERE run_id = ?",
                (json.dumps(leased.as_snapshot()), str(run_id)),
            )
        return leased

    def release_lease(self, run_id: str, owner: str) -> MCPDurableRunState:
        state = self.get(run_id)
        if state is None:
            raise KeyError(f"unknown MCP run {run_id!r}")
        if state.lease_owner != str(owner or "").strip():
            raise ValueError(f"MCP run {run_id!r} is leased by another owner")
        released = MCPDurableRunState(
            **{
                **state.__dict__,
                "lease_owner": None,
                "lease_expires_at": None,
                "updated_at": datetime.now(UTC),
            }
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE mcp_durable_runs SET state_json = ? WHERE run_id = ?",
                (json.dumps(released.as_snapshot()), str(run_id)),
            )
        return released

    def renew_lease(
        self,
        run_id: str,
        owner: str,
        *,
        ttl_seconds: int = 60,
    ) -> MCPDurableRunState:
        if ttl_seconds < 1:
            raise ValueError("lease ttl_seconds must be >= 1")
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM mcp_durable_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown MCP run {run_id!r}")
            state = self._state_from_payload(json.loads(row[0]))
            if state.lease_owner != str(owner or "").strip():
                raise ValueError(f"MCP run {run_id!r} is leased by another owner")
            now = datetime.now(UTC)
            renewed = MCPDurableRunState(
                **{
                    **state.__dict__,
                    "lease_expires_at": now + timedelta(seconds=ttl_seconds),
                    "updated_at": now,
                }
            )
            connection.execute(
                "UPDATE mcp_durable_runs SET state_json = ? WHERE run_id = ?",
                (json.dumps(renewed.as_snapshot()), str(run_id)),
            )
        return renewed

    def list_runs(
        self,
        *,
        state: str | None = None,
        limit: int = 50,
    ) -> list[MCPDurableRunState]:
        bounded_limit = max(1, min(int(limit), 200))
        query = "SELECT state_json FROM mcp_durable_runs"
        params: tuple[Any, ...] = ()
        if state is not None:
            query += " WHERE json_extract(state_json, '$.state') = ?"
            params = (str(state).strip().lower(),)
        query += " ORDER BY json_extract(state_json, '$.updated_at') DESC LIMIT ?"
        params = (*params, bounded_limit)
        with self._lock, self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._state_from_payload(json.loads(payload[0])) for payload in rows]

    @staticmethod
    def _state_from_payload(payload: dict[str, Any]) -> MCPDurableRunState:
        from datetime import datetime

        return MCPDurableRunState(
            run_id=payload["run_id"],
            request_fingerprint=payload.get("request_fingerprint"),
            lease_owner=payload.get("lease_owner"),
            lease_expires_at=(
                datetime.fromisoformat(payload["lease_expires_at"])
                if payload.get("lease_expires_at")
                else None
            ),
            state=payload["state"],
            phase=payload["phase"],
            checkpoint=payload["checkpoint"],
            failure_phase=payload["failure_phase"],
            worker_errors=tuple(payload["worker_errors"]),
            retry_count=payload["retry_count"],
            version=payload.get("version", 0),
            last_sequence=int(payload.get("last_sequence", 0) or 0),
            last_error=payload["last_error"],
            created_at=datetime.fromisoformat(payload["created_at"]),
            updated_at=datetime.fromisoformat(payload["updated_at"]),
        )
