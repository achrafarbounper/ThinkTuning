"""MongoDB Atlas persistence adapters.

The adapters deliberately keep the contracts of the existing SQLite stores.
MongoDB is opt-in (``PERSISTENCE_BACKEND=mongodb``); importing this module does
not connect until a store is instantiated.
"""
from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from core.models import TrainJob
from core.audit_store import MCP_ACTIONS, redact
from app.domain.ports.mcp_ports import MCPSecurityScope


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class MongoConfig:
    """Validated, environment-based Atlas configuration."""

    def __init__(self, uri: str | None = None, database: str | None = None) -> None:
        self.uri = uri or os.getenv("MONGODB_URI", "")
        self.database = database or os.getenv("MONGODB_DATABASE", "thinktuning")
        if not self.uri:
            raise ValueError("MONGODB_URI is required when PERSISTENCE_BACKEND=mongodb")


class MongoClientProvider:
    def __init__(self, config: MongoConfig | None = None) -> None:
        try:
            from pymongo import MongoClient
        except ImportError as exc:  # pragma: no cover - exercised in deployments
            raise RuntimeError("pymongo is required for MongoDB persistence") from exc
        cfg = config or MongoConfig()
        self.client = MongoClient(
            cfg.uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            retryWrites=True,
        )
        self.db = self.client[cfg.database]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        indexes = {
            "agent_sessions": [[("updated_at", -1)]],
            "agent_session_messages": [[("session_id", 1), ("_order", 1)]],
            "agent_runs": [[("created_at", -1)], [("status", 1), ("created_at", -1)]],
            "agent_flows": [[("created_at", -1)], [("status", 1), ("created_at", -1)]],
            "agent_approvals": [[("status", 1), ("created_at", -1)]],
            "agent_audit": [[("ts", -1)], [("action", 1), ("ts", -1)], [("run_id", 1), ("ts", -1)]],
            "jobs": [[("updated_at", -1)], [("payload.status", 1), ("updated_at", -1)]],
            "train_metrics": [[("job_id", 1), ("epoch", 1)]],
            "scheduled_jobs": [[("updated_at", 1)]],
            "mcp_clients": [[("revoked", 1)]],
        }
        for collection_name, specs in indexes.items():
            collection = self.db[collection_name]
            for spec in specs:
                collection.create_index(spec)

    def collection(self, name: str):
        return self.db[name]


_provider: MongoClientProvider | None = None
_provider_lock = threading.Lock()


def get_mongo_provider() -> MongoClientProvider:
    global _provider
    with _provider_lock:
        if _provider is None:
            _provider = MongoClientProvider()
        return _provider


def reset_mongo_provider() -> None:
    global _provider
    with _provider_lock:
        if _provider is not None:
            _provider.client.close()
        _provider = None


class MongoSessionStore:
    def __init__(self, provider: MongoClientProvider | None = None) -> None:
        self.c = (provider or get_mongo_provider()).collection("agent_sessions")
        self.messages = (provider or get_mongo_provider()).collection("agent_session_messages")
        self.memory = (provider or get_mongo_provider()).collection("agent_memory")
        self._lock = threading.RLock()

    def create_session(self, title: str = "", model: str = "") -> dict[str, Any]:
        sid, now = uuid.uuid4().hex[:12], _utcnow()
        doc = {"_id": sid, "id": sid, "title": title.strip() or f"Conversation du {now[:10]}",
               "model": model or "", "created_at": now, "updated_at": now}
        self.c.insert_one(doc)
        return dict(doc)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        d = self.c.find_one({"_id": str(session_id)}, {"_id": 0})
        return d

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        return list(self.c.find({}, {"_id": 0}).sort("updated_at", -1).limit(limit))

    def rename_session(self, session_id: str, title: str) -> dict[str, Any] | None:
        r = self.c.update_one({"_id": str(session_id)}, {"$set": {"title": (title or "").strip()}})
        return self.get_session(session_id) if r.matched_count else None

    def delete_session(self, session_id: str) -> bool:
        r = self.c.delete_one({"_id": str(session_id)})
        self.messages.delete_many({"session_id": str(session_id)})
        return bool(r.deleted_count)

    def append_message(self, session_id: str, role: str, content: str,
                       tool_calls: list[dict[str, Any]] | None = None,
                       thinking: str = "") -> dict[str, Any] | None:
        role = (role or "").strip().lower()
        if role not in ("user", "assistant"):
            raise ValueError(f"Rôle inconnu : '{role}'")
        if not self.c.find_one({"_id": str(session_id)}):
            return None
        doc = {"id": uuid.uuid4().hex, "session_id": str(session_id), "role": role,
               "content": content or "", "thinking": thinking or "", "tool_calls": tool_calls or [],
               "created_at": _utcnow(), "_order": time.time_ns()}
        self.messages.insert_one(doc)
        update: dict[str, Any] = {"updated_at": doc["created_at"]}
        session = self.c.find_one({"_id": str(session_id)})
        if role == "user" and session and str(session.get("title", "")).startswith("Conversation du "):
            update["title"] = (content or "").strip()[:60] or session["title"]
        self.c.update_one({"_id": str(session_id)}, {"$set": update})
        doc.pop("_order", None)
        return doc

    def get_messages(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        rows = list(self.messages.find({"session_id": str(session_id)}, {"_id": 0})
                    .sort("_order", 1).limit(limit))
        return rows

    def save_memory(self, key: str, summary: str) -> None:
        self.memory.update_one({"_id": str(key)}, {"$set": {"key": str(key), "summary": str(summary or ""),
                            "updated_at": _utcnow()}}, upsert=True)

    def get_memory(self, key: str) -> str:
        d = self.memory.find_one({"_id": str(key)})
        return str(d.get("summary", "")) if d else ""

    def delete_memory(self, key: str) -> None:
        self.memory.delete_one({"_id": str(key)})


class MongoRunStore:
    def __init__(self, provider=None):
        self.c = (provider or get_mongo_provider()).collection("agent_runs")
        self._lock = threading.RLock()

    def start_run(self, prompt: str, model: str = "", source: str = "api") -> dict:
        d = {"_id": uuid.uuid4().hex[:12], "id": "", "prompt": prompt or "", "model": model or "",
             "source": source, "status": "running", "answer_summary": "", "error": None,
             "tools": [], "created_at": _utcnow(), "finished_at": None}
        d["id"] = d["_id"]; self.c.insert_one(d); d.pop("_id", None); return d

    def append_tool_event(self, run_id: str, event: dict[str, Any]) -> None:
        self.c.update_one({"_id": str(run_id)}, {"$push": {"tools": {**event, "at": _utcnow()}}})

    def finish_run(self, run_id: str, status: str, answer_summary: str = "", error: str | None = None):
        from core.run_store import STATUSES
        if status not in STATUSES: raise ValueError(f"Statut de run inconnu : '{status}'")
        r = self.c.update_one({"_id": str(run_id)}, {"$set": {"status": status, "answer_summary": answer_summary or "",
            "error": error, "finished_at": _utcnow()}})
        return self.get(run_id) if r.matched_count else None

    def get(self, run_id): return self.c.find_one({"_id": str(run_id)}, {"_id": 0})

    def list(self, limit=50, status=None, tool=None):
        q: dict[str, Any] = {}
        if status: q["status"] = status
        if tool: q["tools.tool"] = tool
        return list(self.c.find(q, {"_id": 0}).sort("created_at", -1).limit(max(1, min(int(limit), 200))))


class MongoFlowStore:
    def __init__(self, provider=None):
        self.c = (provider or get_mongo_provider()).collection("agent_flows")
    def start_flow(self, prompt, model="", source="api"):
        d = {"_id": uuid.uuid4().hex[:12], "id": "", "prompt": prompt or "", "model": model or "", "source": source,
             "status": "running", "answer_summary": "", "error": None, "events": [], "created_at": _utcnow(), "finished_at": None}
        d["id"] = d["_id"]; self.c.insert_one(d); d.pop("_id", None); return d
    def append_event(self, flow_id, event, data, at_ms):
        self.c.update_one({"_id": str(flow_id)}, {"$push": {"events": {"event": event, "data": data, "at_ms": round(at_ms, 2)}}})
    def finish_flow(self, flow_id, status, answer_summary="", error=None):
        from core.flow_store import STATUSES
        if status not in STATUSES: raise ValueError(f"Statut de session inconnu : '{status}'")
        r = self.c.update_one({"_id": str(flow_id)}, {"$set": {"status": status, "answer_summary": answer_summary or "", "error": error, "finished_at": _utcnow()}})
        return self.get(flow_id) if r.matched_count else None
    def get(self, flow_id): return self.c.find_one({"_id": str(flow_id)}, {"_id": 0})
    def list(self, limit=50, status=None):
        q = {"status": status} if status else {}
        out = []
        for d in self.c.find(q, {"_id": 0}).sort("created_at", -1).limit(max(1, min(int(limit), 200))):
            events = d.get("events", [])
            d["tool_calls"] = sum(1 for e in events if e.get("event") in ("agent.worker.tool", "core.tool") and (e.get("data") or {}).get("event") != "tool_result")
            d["agents"] = sorted({str((e.get("data") or {}).get("role")) for e in events if e.get("event") in ("agent.worker.start", "core.start") and (e.get("data") or {}).get("role")})
            d.pop("events", None); out.append(d)
        return out
    def delete(self, flow_id): return bool(self.c.delete_one({"_id": str(flow_id)}).deleted_count)


class MongoApprovalStore:
    def __init__(self, provider=None): self.c = (provider or get_mongo_provider()).collection("agent_approvals")
    def create(self, tool, args, category, decision, reason, prompt="", args_hash="", status="pending"):
        rid = uuid.uuid4().hex
        d = {"_id": rid, "id": rid, "request_id": rid, "tool": str(tool), "args": args if isinstance(args, dict) else {"value": args},
             "category": str(category), "decision": str(decision), "reason": str(reason), "status": status if status in ("pending", "approved", "rejected") else "pending",
             "prompt": str(prompt), "args_hash": str(args_hash), "created_at": _utcnow(), "decided_by": None, "decided_at": None}
        self.c.insert_one(d); d.pop("_id", None); return d
    def get(self, rid): return self.c.find_one({"_id": str(rid)}, {"_id": 0})
    def list(self, status=None): return list(self.c.find({"status": status} if status else {}, {"_id": 0}).sort("created_at", -1))
    def _decide(self, rid, status, decided_by):
        d = self.get(rid)
        if not d: return None
        if d["status"] == "pending": self.c.update_one({"_id": str(rid)}, {"$set": {"status": status, "decided_by": decided_by, "decided_at": _utcnow()}})
        return self.get(rid)
    def approve(self, rid, decided_by=None): return self._decide(rid, "approved", decided_by)
    def reject(self, rid, decided_by=None): return self._decide(rid, "rejected", decided_by)


class MongoAuditStore:
    def __init__(self, provider=None): self.c = (provider or get_mongo_provider()).collection("agent_audit")
    def log(self, action, subject="", detail=None, actor="system", ip=None, request_id=None, run_id=None):
        d = {"_id": str(uuid.uuid4()), "id": "", "ts": _utcnow(), "actor": actor or "system", "action": action, "subject": subject or "",
             "detail": redact(detail or {}), "ip": ip, "request_id": request_id or "", "run_id": run_id or ""}
        d["id"] = d["_id"]; self.c.insert_one(d); d.pop("_id", None); return d
    def get(self, aid): return self.c.find_one({"_id": str(aid)}, {"_id": 0})
    def query(self, action=None, subject=None, actor=None, run_id=None, limit=100, offset=0):
        q = {k: v for k, v in (("action", action), ("subject", subject), ("actor", actor), ("run_id", run_id)) if v}
        total = self.c.count_documents(q); rows = list(self.c.find(q, {"_id": 0}).sort([("ts", -1), ("id", 1)]).skip(max(0, int(offset))).limit(max(1, min(int(limit), 500))))
        return {"items": rows, "total": total, "limit": max(1, min(int(limit), 500)), "offset": max(0, int(offset))}
    def mcp_metrics(self):
        rows = list(self.c.find({"action": {"$in": list(MCP_ACTIONS)}}))
        by = {a: 0 for a in MCP_ACTIONS}
        for r in rows: by[r["action"]] += 1
        errors = sum(1 for r in rows if (r.get("detail") or {}).get("is_error") is True)
        return {"total": len(rows), "by_action": by, "errors": errors, "error_rate": round(errors / len(rows), 4) if rows else 0.0}


class MongoAgentSettingsStore:
    def __init__(self, provider=None): self.c = (provider or get_mongo_provider()).collection("agent_settings")
    def get_all(self): return {d["key"]: d.get("value") for d in self.c.find({}, {"_id": 0})}
    def save_many(self, values):
        from core.agent_settings import SETTING_KEYS
        filtered = {k: values[k] for k in SETTING_KEYS if k in values}
        for k, v in filtered.items(): self.c.update_one({"_id": k}, {"$set": {"key": k, "value": v, "updated_at": time.time()}}, upsert=True)
        return filtered


class MongoJobStore(dict):
    """Dict-compatible job store with Mongo persistence."""
    def __init__(self, provider=None):
        super().__init__(); self.c = (provider or get_mongo_provider()).collection("jobs"); self.metrics = (provider or get_mongo_provider()).collection("train_metrics"); self.schedules = (provider or get_mongo_provider()).collection("scheduled_jobs"); self._load()
    def _load(self):
        for d in self.c.find({}, {"_id": 0}):
            self[d["job_id"]] = TrainJob(**d["payload"])
    def __setitem__(self, key, value):
        if not isinstance(value, TrainJob): raise TypeError("MongoJobStore accepts only TrainJob instances.")
        super().__setitem__(key, value); self.c.update_one({"_id": str(key)}, {"$set": {"job_id": str(key), "payload": value.model_dump(mode="json"), "updated_at": time.time()}}, upsert=True)
    def get(self, key, default=None):
        value = super().get(key)
        if value is not None: return value
        d = self.c.find_one({"_id": str(key)})
        if not d: return default
        value = TrainJob(**d["payload"]); super().__setitem__(key, value); return value
    def list_jobs(self, status=None, kind=None, limit=100, offset=0):
        q = {};
        if status: q["payload.status"] = status
        if kind: q["payload.kind"] = kind
        total = self.c.count_documents(q)
        rows = list(self.c.find(q, {"_id": 0}).sort("payload.started_at", -1).skip(max(0, offset)).limit(max(1, limit)))
        return [TrainJob(**d["payload"]) for d in rows], total
    def save_epoch_metrics(self, job_id, records):
        for r in records: self.metrics.update_one({"job_id": job_id, "epoch": int(r.get("epoch", 0))}, {"$set": {**r, "job_id": job_id, "updated_at": time.time()}}, upsert=True)
    def get_job_metrics(self, job_id): return list(self.metrics.find({"job_id": job_id}, {"_id": 0}).sort("epoch", 1))
    def save_schedule(self, schedule): self.schedules.update_one({"_id": schedule["schedule_id"]}, {"$set": schedule}, upsert=True)
    def get_schedules(self): return list(self.schedules.find({}, {"_id": 0}).sort("updated_at", 1))
    def get_schedule(self, schedule_id): return self.schedules.find_one({"_id": schedule_id}, {"_id": 0})
    def delete_schedule(self, schedule_id): return bool(self.schedules.delete_one({"_id": schedule_id}).deleted_count)
    def remove_job(self, job_id): self.metrics.delete_many({"job_id": job_id}); self.pop(job_id, None); return bool(self.c.delete_one({"_id": job_id}).deleted_count)
    def update_job_timestamp(self, job_id, timestamp): return bool(self.c.update_one({"_id": job_id}, {"$set": {"updated_at": timestamp}}).matched_count)
    def cleanup_old_jobs(self, max_age_days=30, dry_run=False):
        ids = [d["job_id"] for d in self.c.find({"updated_at": {"$lt": time.time() - max_age_days * 86400}, "payload.status": {"$in": ["completed", "failed", "cancelled"]}}, {"job_id": 1})]
        if not dry_run:
            for i in ids: self.remove_job(i)
        return {"deleted": 0 if dry_run else len(ids), "job_ids": ids}


class MongoMCPClientStore:
    def __init__(self, provider=None): self.c = (provider or get_mongo_provider()).collection("mcp_clients")
    @staticmethod
    def _hash_secret(secret): return hashlib.sha256(secret.encode()).hexdigest()
    def register(self, client_id, secret, scope):
        from core.mcp_client_store import MCPClientAlreadyExistsError
        if not client_id or not secret: raise ValueError("client_id et secret sont obligatoires")
        if self.c.find_one({"_id": client_id}): raise MCPClientAlreadyExistsError(client_id)
        now = _utcnow(); d = {"_id": client_id, "client_id": client_id, "secret_hash": self._hash_secret(secret), "scope": scope.model_dump(), "call_count": 0, "error_count": 0, "scope_usage": {}, "created_at": now, "updated_at": now, "revoked": False, "revoked_at": None, "revoked_reason": ""}
        self.c.insert_one(d); return self._public(d)
    def _public(self, d):
        s = MCPSecurityScope.model_validate(d["scope"]); return {"client_id": d["client_id"], **s.model_dump(), "revoked": d["revoked"], "revoked_at": d["revoked_at"], "revoked_reason": d["revoked_reason"], "call_count": d["call_count"], "error_count": d["error_count"], "scope_usage": d["scope_usage"], "created_at": d["created_at"], "updated_at": d["updated_at"]}
    def get_by_client_id(self, cid): d = self.c.find_one({"_id": cid}); return self._public(d) if d else None
    def list(self): return [self._public(d) for d in self.c.find({})]
    def revoke(self, cid, reason):
        from core.mcp_client_store import MCPClientNotFoundError, MCPClientRevokedError
        d = self.c.find_one({"_id": cid})
        if not d: raise MCPClientNotFoundError(cid)
        if d["revoked"]: raise MCPClientRevokedError(cid)
        self.c.update_one({"_id": cid}, {"$set": {"revoked": True, "revoked_at": _utcnow(), "revoked_reason": reason or "", "updated_at": _utcnow()}})
        return self.get_by_client_id(cid)
    def get_scope(self, cid):
        from core.mcp_client_store import MCPClientNotFoundError
        d = self.c.find_one({"_id": cid})
        if not d: raise MCPClientNotFoundError(cid)
        return MCPSecurityScope.model_validate(d["scope"])
    def authenticate(self, cid, secret):
        d = self.c.find_one({"_id": cid}); return bool(d and not d["revoked"] and d["secret_hash"] == self._hash_secret(secret))
    def record_call(self, cid, tool_name, success):
        self.c.update_one({"_id": cid}, {"$inc": {"call_count": 1, "error_count": 0 if success else 1, f"scope_usage.{tool_name}": 1}, "$set": {"updated_at": _utcnow()}})
    def metrics(self, cid):
        d = self.c.find_one({"_id": cid}); calls = int(d.get("call_count", 0)) if d else 0; errors = int(d.get("error_count", 0)) if d else 0
        return {"call_count": calls, "error_count": errors, "error_rate": round(errors / calls, 4) if calls else 0.0, "scope_usage": d.get("scope_usage", {}) if d else {}}
    def count(self): return self.c.count_documents({})
    def count_active(self): return self.c.count_documents({"revoked": False})
    def count_revoked(self): return self.c.count_documents({"revoked": True})
