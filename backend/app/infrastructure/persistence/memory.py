"""Fakes en mémoire conformes aux ports (tests des use-cases sans disque).

Mêmes sémantiques que les stores SQLite legacy :
    - ``MemoryRunStore``  : cycle de vie running → statut final, filtres
      ``status`` / ``tool``, borne ``limit`` (les plus récents d'abord) ;
    - ``MemoryApprovalStore`` : ``create`` renvoie la LIGNE (convention
      ``request_id``), ``approve``/``reject`` ne transforment qu'une demande
      ``pending`` (une demande inconnue renvoie ``None``, une demande déjà
      décidée est renvoyée inchangée — la route mappe en 409).

Concurrence : verrou unique (les use-cases peuvent tourner en threads).
"""

from __future__ import annotations

import threading
import uuid
from typing import Any

RUNNING = "running"
COMPLETED = "completed"
AWAITING_APPROVAL = "awaiting_approval"
REJECTED = "rejected"
ERROR = "error"
RUN_STATUSES = {COMPLETED, AWAITING_APPROVAL, REJECTED, ERROR, RUNNING}

PENDING = "pending"
APPROVED = "approved"


class MemoryRunStore:
    """``RunStorePort`` en mémoire (deterministe, insensible aux horloges)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def start_run(
        self, prompt: str, model: str = "", source: str = "api"
    ) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            run_id = f"run-{self._seq:04d}-{uuid.uuid4().hex[:6]}"
            row = {
                "id": run_id,
                "prompt": prompt or "",
                "model": model or "",
                "source": source,
                "status": RUNNING,
                "answer_summary": "",
                "error": None,
                "tools": [],
            }
            self._runs[run_id] = row
            return dict(row)

    def append_tool_event(self, run_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            row = self._runs.get(str(run_id))
            if row is not None:
                row["tools"].append(dict(event))

    def finish_run(
        self,
        run_id: str,
        status: str,
        answer_summary: str = "",
        error: str | None = None,
    ) -> dict[str, Any] | None:
        if status not in RUN_STATUSES:
            raise ValueError(f"Statut de run inconnu : '{status}'")
        with self._lock:
            row = self._runs.get(str(run_id))
            if row is None:
                return None
            row["status"] = status
            row["answer_summary"] = answer_summary or ""
            row["error"] = error
            return dict(row)

    def get(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._runs.get(str(run_id))
            return dict(row) if row else None

    def list(
        self,
        limit: int = 50,
        status: str | None = None,
        tool: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = [
                dict(row) for row in reversed(list(self._runs.values()))
                if status is None or row["status"] == status
            ]
        if tool is not None:
            rows = [r for r in rows if any(
                ev.get("tool") == tool for ev in r["tools"]
            )]
        # Contrat legacy : limit <= 0 = non borné (le SQLite legacy n'émet
        # aucune clause LIMIT dans ce cas).
        return rows if limit <= 0 else rows[:limit]


class MemoryApprovalStore:
    """``ApprovalStorePort`` en mémoire (convention ``create`` : ligne renvoyée)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}
        self._seq = 0

    def create(
        self,
        tool: str,
        args: Any,
        category: str,
        decision: str,
        reason: str,
        prompt: str = "",
        args_hash: str = "",
        status: str = PENDING,
    ) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            request_id = f"req-{self._seq:04d}-{uuid.uuid4().hex[:6]}"
            record = {
                "request_id": request_id,
                "id": request_id,
                "tool": str(tool),
                "args": args,
                "category": str(category),
                "decision": str(decision),
                "reason": str(reason),
                "status": status if status in (PENDING, REJECTED) else PENDING,
                "prompt": str(prompt),
                "args_hash": str(args_hash),
                "decided_by": None,
            }
            self._records[request_id] = record
            return dict(record)

    def get(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(str(request_id))
            return dict(record) if record else None

    def _decide(
        self, request_id: str, new_status: str, decided_by: str | None
    ) -> dict[str, Any] | None:
        with self._lock:
            record = self._records.get(str(request_id))
            if record is None:
                return None
            if record["status"] == PENDING:
                record["status"] = new_status
                record["decided_by"] = decided_by
            return dict(record)

    def approve(
        self, request_id: str, decided_by: str | None = None
    ) -> dict[str, Any] | None:
        return self._decide(request_id, APPROVED, decided_by)

    def reject(
        self, request_id: str, decided_by: str | None = None
    ) -> dict[str, Any] | None:
        return self._decide(request_id, REJECTED, decided_by)

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            rows = [dict(r) for r in self._records.values()]
        if status is not None:
            rows = [r for r in rows if r["status"] == status]
        return rows
