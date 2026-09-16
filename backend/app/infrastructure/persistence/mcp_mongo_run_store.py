"""MongoDB persistence for MCP durable orchestration runs."""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo.errors import DuplicateKeyError, OperationFailure

from app.domain.ports.mcp_ports import MCPDurableRunState
from app.infrastructure.persistence.mongodb import MongoClientProvider, get_mongo_provider


class MongoMCPDurableRunStore:
    """Mongo implementation of the durable-run port.

    Run snapshots and ordered events use separate collections so event
    retention can evolve independently from the lifecycle document.
    """

    EVENT_RETENTION_INDEX = "mcp_durable_events_created_at_ttl"
    EVENT_ID_INDEX = "mcp_durable_events_run_id_event_id"
    DEFAULT_EVENT_RETENTION_DAYS = 30

    def __init__(
        self,
        provider: MongoClientProvider | None = None,
        *,
        retention_days: int | None = None,
    ) -> None:
        provider = provider or get_mongo_provider()
        self.runs = provider.collection("mcp_durable_runs")
        self.events = provider.collection("mcp_durable_events")
        self.runs.create_index("updated_at")
        self.runs.create_index("state")
        self.events.create_index([("run_id", 1), ("sequence", 1)], unique=True)
        self._ensure_event_id_index()
        configured_retention = (
            retention_days
            if retention_days is not None
            else int(
                os.getenv(
                    "MCP_EVENT_RETENTION_DAYS",
                    str(self.DEFAULT_EVENT_RETENTION_DAYS),
                )
            )
        )
        if configured_retention < 0:
            raise ValueError("MCP event retention days must be >= 0")
        if configured_retention == 0:
            if self.EVENT_RETENTION_INDEX in self.events.index_information():
                self.events.drop_index(self.EVENT_RETENTION_INDEX)
        else:
            self.events.create_index(
                "created_at",
                name=self.EVENT_RETENTION_INDEX,
                expireAfterSeconds=configured_retention * 86400,
            )

    def _ensure_event_id_index(self) -> None:
        """Keep event-id uniqueness limited to events that have an identifier."""
        expected_key = [("run_id", 1), ("event_id", 1)]
        # `$ne: null` is not supported in MongoDB partial-index expressions.
        # Events are normalized to string identifiers before insertion, so
        # `$type: string` excludes both missing and legacy null values.
        expected_filter = {"event_id": {"$type": "string"}}
        indexes = self.events.index_information()
        existing = indexes.get(self.EVENT_ID_INDEX)
        if existing is not None and (
            existing.get("key") != expected_key
            or not existing.get("unique")
            or existing.get("partialFilterExpression") != expected_filter
        ):
            self.events.drop_index(self.EVENT_ID_INDEX)

        for name, metadata in indexes.items():
            if name == self.EVENT_ID_INDEX or name == "_id_":
                continue
            if (
                metadata.get("key") == expected_key
                and metadata.get("unique")
                and metadata.get("sparse")
            ):
                self.events.drop_index(name)

        self.events.create_index(
            expected_key,
            name=self.EVENT_ID_INDEX,
            unique=True,
            partialFilterExpression=expected_filter,
        )

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
        document = state.as_snapshot()
        document["_id"] = normalized_id
        try:
            self.runs.insert_one(document)
        except DuplicateKeyError as exc:
            raise ValueError(f"run {normalized_id!r} already exists") from exc
        return state

    def get(self, run_id: str) -> MCPDurableRunState | None:
        document = self.runs.find_one({"_id": str(run_id)})
        return self._from_document(document) if document else None

    def transition(self, run_id: str, new_state: str, **kwargs: Any) -> MCPDurableRunState:
        current = self.get(run_id)
        if current is None:
            raise KeyError(f"unknown MCP run {run_id!r}")
        updated = current.transition(new_state, **kwargs)
        version_filter: dict[str, Any] = {"version": current.version}
        if current.version == 0:
            version_filter = {
                "$or": [
                    {"version": 0},
                    {"version": {"$exists": False}},
                ]
            }
        result = self.runs.replace_one(
            {"_id": str(run_id), **version_filter},
            self._document(updated),
        )
        if result.matched_count != 1:
            if self.get(run_id) is None:
                raise KeyError(f"unknown MCP run {run_id!r}")
            raise ValueError(f"MCP run {run_id!r} was modified concurrently")
        return updated

    def append_event(self, run_id: str, event: dict[str, Any]) -> int:
        """Persiste un événement et retourne sa séquence (monotone).

        L1 (SCRUM-152) : le curseur ``last_sequence`` du run est mis à jour
        dans la même opération atomique que le compteur — un client rejoué
        reprend avec ``after_sequence=last_sequence``.
        """
        if self.get(run_id) is None:
            raise KeyError(f"unknown MCP run {run_id!r}")
        event_id = event.get("event_id")
        if event_id is not None:
            existing = self.events.find_one(
                {"run_id": str(run_id), "event_id": str(event_id)},
                projection={"sequence": 1},
            )
            if existing is not None:
                return int(existing.get("sequence", 0))
        try:
            from pymongo import ReturnDocument

            latest_event = self.events.find_one(
                {"run_id": str(run_id)},
                sort=[("sequence", -1)],
                projection={"sequence": 1},
            )
            latest_sequence = int(latest_event["sequence"]) if latest_event else 0
            counter = self.runs.find_one_and_update(
                {"_id": str(run_id)},
                [
                    {
                        "$set": {
                            "event_sequence": {
                                "$add": [
                                    {
                                        "$max": [
                                            {"$ifNull": ["$event_sequence", 0]},
                                            latest_sequence,
                                        ]
                                    },
                                    1,
                                ]
                            }
                        }
                    },
                    {
                        "$set": {
                            "last_sequence": {
                                "$max": [
                                    {"$ifNull": ["$last_sequence", 0]},
                                    "$event_sequence",
                                ]
                            }
                        }
                    },
                ],
                return_document=ReturnDocument.AFTER,
            )
        except ImportError as exc:  # pragma: no cover - dependency is runtime-required
            raise RuntimeError("pymongo is required for Mongo durable events") from exc
        if counter is None:
            raise KeyError(f"unknown MCP run {run_id!r}")
        sequence = int(counter.get("event_sequence", 1))
        document = {
            "_id": uuid.uuid4().hex,
            "run_id": str(run_id),
            "sequence": sequence,
            "event": dict(event),
            "created_at": datetime.now(UTC),
        }
        if event_id is not None:
            document["event_id"] = str(event_id)
        try:
            self.events.insert_one(document)
        except DuplicateKeyError as exc:
            if event_id is not None:
                return sequence
            raise exc
        except OperationFailure as exc:
            if exc.code == 11000 and event_id is not None:
                return sequence
            raise
        return sequence

    def last_sequence(self, run_id: str) -> int:
        """Dernière séquence persistée : max du curseur run et des événements."""
        cursored = 0
        document = self.runs.find_one({"_id": str(run_id)}, projection={"last_sequence": 1})
        if document is not None:
            cursored = int(document.get("last_sequence") or 0)
        latest = self.events.find_one(
            {"run_id": str(run_id)},
            sort=[("sequence", -1)],
            projection={"sequence": 1},
        )
        return max(cursored, int(latest["sequence"]) if latest else 0)

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        return [
            dict(document["event"])
            for document in self.events.find({"run_id": str(run_id)}).sort("sequence", 1)
        ]

    def list_events_after(self, run_id: str, after_sequence: int = 0) -> list[dict[str, Any]]:
        if int(after_sequence) < 0:
            raise ValueError("after_sequence must be >= 0")
        return [
            {"sequence": int(document["sequence"]), **dict(document["event"])}
            for document in self.events.find(
                {"run_id": str(run_id), "sequence": {"$gt": int(after_sequence)}}
            ).sort("sequence", 1)
        ]

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
        if not str(owner or "").strip():
            raise ValueError("lease owner must not be empty")
        if ttl_seconds < 1:
            raise ValueError("lease ttl_seconds must be >= 1")
        now = datetime.now(UTC)
        state = self.get(run_id)
        if state is None:
            raise KeyError(f"unknown MCP run {run_id!r}")
        if (
            state.lease_owner
            and state.lease_owner != owner
            and state.lease_expires_at
            and state.lease_expires_at > now
        ):
            raise ValueError(f"MCP run {run_id!r} is leased by another owner")
        leased = MCPDurableRunState(
            **{
                **state.__dict__,
                "lease_owner": owner,
                "lease_expires_at": now + timedelta(seconds=ttl_seconds),
                "version": state.version + 1,
                "updated_at": now,
            }
        )
        try:
            from pymongo import ReturnDocument

            version_filter: dict[str, Any] = {"version": state.version}
            if state.version == 0:
                version_filter = {
                    "$or": [
                        {"version": 0},
                        {"version": {"$exists": False}},
                    ]
                }
            result = self.runs.find_one_and_replace(
                {
                    "_id": str(run_id),
                    "$and": [
                        {
                            "$or": [
                                {"lease_owner": {"$exists": False}},
                                {"lease_owner": None},
                                {"lease_owner": owner},
                                {"lease_expires_at": {"$lte": now}},
                            ]
                        },
                        version_filter,
                    ],
                },
                self._document(leased),
                return_document=ReturnDocument.AFTER,
            )
        except ImportError as exc:  # pragma: no cover - dependency is runtime-required
            raise RuntimeError("pymongo is required for Mongo durable leases") from exc
        if result is None:
            raise ValueError(f"MCP run {run_id!r} is leased by another owner")
        return leased

    def renew_lease(self, run_id: str, owner: str, *, ttl_seconds: int = 60) -> MCPDurableRunState:
        if ttl_seconds < 1:
            raise ValueError("lease ttl_seconds must be >= 1")
        state = self.get(run_id)
        if state is None:
            raise KeyError(f"unknown MCP run {run_id!r}")
        if state.lease_owner != str(owner or "").strip():
            raise ValueError(f"MCP run {run_id!r} is leased by another owner")
        now = datetime.now(UTC)
        renewed = MCPDurableRunState(
            **{
                **state.__dict__,
                "lease_expires_at": now + timedelta(seconds=ttl_seconds),
                "version": state.version + 1,
                "updated_at": now,
            }
        )
        result = self.runs.replace_one(
            {"_id": str(run_id), "version": state.version, "lease_owner": owner},
            self._document(renewed),
        )
        if result.matched_count != 1:
            raise ValueError(f"MCP run {run_id!r} was modified concurrently")
        return renewed

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
                "version": state.version + 1,
                "updated_at": datetime.now(UTC),
            }
        )
        result = self.runs.replace_one(
            {"_id": str(run_id), "version": state.version, "lease_owner": owner},
            self._document(released),
        )
        if result.matched_count != 1:
            raise ValueError(f"MCP run {run_id!r} was modified concurrently")
        return released

    def list_runs(
        self,
        *,
        state: str | None = None,
        limit: int = 50,
    ) -> list[MCPDurableRunState]:
        query = {"state": str(state).strip().lower()} if state else {}
        return [
            self._from_document(document)
            for document in self.runs.find(query)
            .sort("updated_at", -1)
            .limit(max(1, min(int(limit), 200)))
        ]

    @staticmethod
    def _document(state: MCPDurableRunState) -> dict[str, Any]:
        return {"_id": state.run_id, **state.as_snapshot()}

    @staticmethod
    def _from_document(document: dict[str, Any]) -> MCPDurableRunState:
        payload = dict(document)
        payload.pop("_id", None)
        payload.pop("event_sequence", None)
        for key in ("created_at", "updated_at", "lease_expires_at"):
            value = payload.get(key)
            if isinstance(value, datetime):
                payload[key] = value.isoformat()
        payload["worker_errors"] = tuple(payload.get("worker_errors") or ())
        return MCPDurableRunState(
            **{
                **payload,
                "created_at": datetime.fromisoformat(payload["created_at"]),
                "updated_at": datetime.fromisoformat(payload["updated_at"]),
                "lease_expires_at": (
                    datetime.fromisoformat(payload["lease_expires_at"])
                    if payload.get("lease_expires_at")
                    else None
                ),
            }
        )
