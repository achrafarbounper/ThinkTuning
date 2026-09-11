"""MongoDB Atlas persistence adapters.

MongoDB is the runtime persistence backend.  ``MongoClientProvider`` also
accepts an injected client, which is the supported seam for mongomock tests;
tests therefore never need a network connection.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from app.domain.ports.mcp_ports import MCPSecurityScope
from core.audit_store import MCP_ACTIONS, redact
from core.models import TrainJob


def _utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class MongoConfig:
    """Validated, environment-based Atlas configuration.

    Source unique : ``backend/.env`` (gitignoré) chargé paresseusement ici
    aussi — importer ``persistence.mongodb`` directement (sans passer par
    ``app.config.settings``) doit quand même voir ``MONGODB_URI``. En dernier
    recours, repli sur ``Settings`` (pydantic-settings lit l'env + `.env`).
    Priorité : arg explicite > variable d'environnement > Settings.
    """

    def __init__(self, uri: str | None = None, database: str | None = None) -> None:
        if not uri:
            try:
                from app.config.settings import _load_dotenv_to_environ

                _load_dotenv_to_environ()
            except Exception:
                pass
        settings_uri: str | None = None
        settings_db: str | None = None
        if not os.getenv("MONGODB_URI", "") or not os.getenv("MONGODB_DATABASE", ""):
            try:
                from app.config.settings import get_settings

                _s = get_settings()
                settings_uri = _s.mongodb_uri
                settings_db = _s.mongodb_database
            except Exception:
                pass
        self.uri = uri or os.getenv("MONGODB_URI", "") or settings_uri or ""
        self.database = (
            database or os.getenv("MONGODB_DATABASE", "") or settings_db or "thinktuning"
        )
        if not self.uri:
            raise ValueError("MONGODB_URI is required for MongoDB persistence")


class MongoClientProvider:
    def __init__(self, config: MongoConfig | None = None, client: Any | None = None) -> None:
        if client is not None:
            self.client = client
            cfg = config
            database = cfg.database if cfg else os.getenv("MONGODB_DATABASE", "thinktuning")
            self.db = client[database]
            self._ensure_indexes()
            return
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
            if os.getenv("MONGODB_MOCK", "").lower() in {"1", "true", "yes"}:
                try:
                    import mongomock
                except ImportError as exc:  # pragma: no cover - deployment guard
                    raise RuntimeError(
                        "mongomock is required when MONGODB_MOCK is enabled"
                    ) from exc
                _provider = MongoClientProvider(
                    MongoConfig(uri="mongodb://localhost/thinktuning"),
                    client=mongomock.MongoClient(),
                )
            else:
                _provider = MongoClientProvider()
        return _provider


def set_mongo_provider(provider: MongoClientProvider | None) -> None:
    """Inject a provider for tests and close the previous provider safely."""
    global _provider
    with _provider_lock:
        if _provider is not None and _provider is not provider:
            _provider.client.close()
        _provider = provider


def reset_mongo_provider() -> None:
    global _provider
    with _provider_lock:
        if _provider is not None:
            _provider.client.close()
        _provider = None


class MongoSessionStore:
    def __init__(self, provider: MongoClientProvider | None = None) -> None:
        provider = provider or get_mongo_provider()
        self.c = provider.collection("agent_sessions")
        self.messages = provider.collection("agent_session_messages")
        self.memory = provider.collection("agent_memory")
        self._lock = threading.RLock()

    def create_session(self, title: str = "", model: str = "") -> dict[str, Any]:
        sid, now = uuid.uuid4().hex[:12], _utcnow()
        doc = {
            "_id": sid,
            "id": sid,
            "title": title.strip() or f"Conversation du {now[:10]}",
            "model": model or "",
            "created_at": now,
            "updated_at": now,
        }
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

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
        thinking: str = "",
    ) -> dict[str, Any] | None:
        role = (role or "").strip().lower()
        if role not in ("user", "assistant"):
            raise ValueError(f"Rôle inconnu : '{role}'")
        if not self.c.find_one({"_id": str(session_id)}):
            return None
        doc = {
            "id": uuid.uuid4().hex,
            "session_id": str(session_id),
            "role": role,
            "content": content or "",
            "thinking": thinking or "",
            "tool_calls": tool_calls or [],
            "created_at": _utcnow(),
            "_order": time.time_ns(),
        }
        self.messages.insert_one(doc)
        update: dict[str, Any] = {"updated_at": doc["created_at"]}
        session = self.c.find_one({"_id": str(session_id)})
        if (
            role == "user"
            and session
            and str(session.get("title", "")).startswith("Conversation du ")
        ):
            update["title"] = (content or "").strip()[:60] or session["title"]
        self.c.update_one({"_id": str(session_id)}, {"$set": update})
        doc.pop("_order", None)
        return doc

    def get_messages(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        rows = list(
            self.messages.find({"session_id": str(session_id)}, {"_id": 0})
            .sort("_order", 1)
            .limit(limit)
        )
        for row in rows:
            row.pop("_order", None)
        return rows

    def save_memory(self, key: str, summary: str) -> None:
        self.memory.update_one(
            {"_id": str(key)},
            {
                "$set": {
                    "key": str(key),
                    "summary": str(summary or ""),
                    "updated_at": _utcnow(),
                }
            },
            upsert=True,
        )

    def get_memory(self, key: str) -> str:
        d = self.memory.find_one({"_id": str(key)})
        return str(d.get("summary", "")) if d else ""

    def delete_memory(self, key: str) -> None:
        self.memory.delete_one({"_id": str(key)})


class MongoRunStore:
    def __init__(self, provider=None, *args, **kwargs):
        db = provider if hasattr(provider, "collection") else get_mongo_provider()
        self.c = db.collection("agent_runs")
        self._lock = threading.RLock()

    def start_run(self, prompt: str, model: str = "", source: str = "api") -> dict:
        d = {
            "_id": uuid.uuid4().hex[:12],
            "id": "",
            "prompt": prompt or "",
            "model": model or "",
            "source": source,
            "status": "running",
            "answer_summary": "",
            "error": None,
            "tools": [],
            "created_at": _utcnow(),
            "finished_at": None,
        }
        d["id"] = d["_id"]
        self.c.insert_one(d)
        d.pop("_id", None)
        return d

    def append_tool_event(self, run_id: str, event: dict[str, Any]) -> None:
        self.c.update_one(
            {"_id": str(run_id)},
            {"$push": {"tools": {**event, "at": _utcnow()}}},
        )

    def finish_run(
        self,
        run_id: str,
        status: str,
        answer_summary: str = "",
        error: str | None = None,
    ):
        from core.run_store import STATUSES

        if status not in STATUSES:
            raise ValueError(f"Statut de run inconnu : '{status}'")
        r = self.c.update_one(
            {"_id": str(run_id)},
            {
                "$set": {
                    "status": status,
                    "answer_summary": answer_summary or "",
                    "error": error,
                    "finished_at": _utcnow(),
                }
            },
        )
        return self.get(run_id) if r.matched_count else None

    def get(self, run_id):
        return self.c.find_one({"_id": str(run_id)}, {"_id": 0})

    def list(self, limit=50, status=None, tool=None):
        q: dict[str, Any] = {}
        if status:
            q["status"] = status
        if tool:
            q["tools.tool"] = tool
        cursor = (
            self.c.find(q, {"_id": 0}).sort("created_at", -1).limit(max(1, min(int(limit), 200)))
        )
        return list(cursor)


class MongoFlowStore:
    def __init__(self, provider=None, *args, **kwargs):
        db = provider if hasattr(provider, "collection") else get_mongo_provider()
        self.c = db.collection("agent_flows")

    def start_flow(self, prompt, model="", source="api"):
        d = {
            "_id": uuid.uuid4().hex[:12],
            "id": "",
            "prompt": prompt or "",
            "model": model or "",
            "source": source,
            "status": "running",
            "answer_summary": "",
            "error": None,
            "events": [],
            "created_at": _utcnow(),
            "finished_at": None,
        }
        d["id"] = d["_id"]
        self.c.insert_one(d)
        d.pop("_id", None)
        return d

    def append_event(self, flow_id, event, data, at_ms):
        self.c.update_one(
            {"_id": str(flow_id)},
            {
                "$push": {
                    "events": {
                        "event": event,
                        "data": data,
                        "at_ms": round(at_ms, 2),
                    }
                }
            },
        )

    def finish_flow(self, flow_id, status, answer_summary="", error=None):
        from core.flow_store import STATUSES

        if status not in STATUSES:
            raise ValueError(f"Statut de session inconnu : '{status}'")
        r = self.c.update_one(
            {"_id": str(flow_id)},
            {
                "$set": {
                    "status": status,
                    "answer_summary": answer_summary or "",
                    "error": error,
                    "finished_at": _utcnow(),
                }
            },
        )
        return self.get(flow_id) if r.matched_count else None

    def get(self, flow_id):
        return self.c.find_one({"_id": str(flow_id)}, {"_id": 0})

    def list(self, limit=50, status=None):
        q = {"status": status} if status else {}
        out = []
        cursor = (
            self.c.find(q, {"_id": 0}).sort("created_at", -1).limit(max(1, min(int(limit), 200)))
        )
        for d in cursor:
            events = d.get("events", [])
            d["tool_calls"] = sum(
                1
                for e in events
                if e.get("event") in ("agent.worker.tool", "core.tool")
                and (e.get("data") or {}).get("event") != "tool_result"
            )
            d["agents"] = sorted(
                {
                    str((e.get("data") or {}).get("role"))
                    for e in events
                    if e.get("event") in ("agent.worker.start", "core.start")
                    and (e.get("data") or {}).get("role")
                }
            )
            d.pop("events", None)
            out.append(d)
        return out

    def delete(self, flow_id):
        return bool(self.c.delete_one({"_id": str(flow_id)}).deleted_count)


class MongoApprovalStore:
    def __init__(self, provider=None, *args, **kwargs):
        db = provider if hasattr(provider, "collection") else get_mongo_provider()
        self.c = db.collection("agent_approvals")

    def create(
        self,
        tool,
        args,
        category,
        decision,
        reason,
        prompt="",
        args_hash="",
        status="pending",
    ):
        rid = uuid.uuid4().hex
        valid_statuses = ("pending", "approved", "rejected")
        d = {
            "_id": rid,
            "id": rid,
            "request_id": rid,
            "tool": str(tool),
            "args": args if isinstance(args, dict) else {"value": args},
            "category": str(category),
            "decision": str(decision),
            "reason": str(reason),
            "status": status if status in valid_statuses else "pending",
            "prompt": str(prompt),
            "args_hash": str(args_hash),
            "created_at": _utcnow(),
            "decided_by": None,
            "decided_at": None,
        }
        self.c.insert_one(d)
        d.pop("_id", None)
        return d

    def get(self, rid):
        return self.c.find_one({"_id": str(rid)}, {"_id": 0})

    def list(self, status=None):
        q = {"status": status} if status else {}
        return list(self.c.find(q, {"_id": 0}).sort("created_at", -1))

    def _decide(self, rid, status, decided_by):
        d = self.get(rid)
        if not d:
            return None
        if d["status"] == "pending":
            self.c.update_one(
                {"_id": str(rid)},
                {
                    "$set": {
                        "status": status,
                        "decided_by": decided_by,
                        "decided_at": _utcnow(),
                    }
                },
            )
        return self.get(rid)

    def approve(self, request_id, decided_by=None):
        rid = request_id
        return self._decide(rid, "approved", decided_by)

    def reject(self, rid, decided_by=None):
        return self._decide(rid, "rejected", decided_by)


class MongoAuditStore:
    def __init__(self, provider=None, *args, **kwargs):
        db = provider if hasattr(provider, "collection") else get_mongo_provider()
        self.c = db.collection("agent_audit")

    def log(
        self,
        action,
        subject="",
        detail=None,
        actor="system",
        ip=None,
        request_id=None,
        run_id=None,
    ):
        d = {
            "_id": str(uuid.uuid4()),
            "id": "",
            "ts": _utcnow(),
            "actor": actor or "system",
            "action": action,
            "subject": subject or "",
            "detail": redact(detail or {}),
            "ip": ip,
            "request_id": request_id or "",
            "run_id": run_id or "",
        }
        d["id"] = d["_id"]
        self.c.insert_one(d)
        d.pop("_id", None)
        return d

    def get(self, aid):
        return self.c.find_one({"_id": str(aid)}, {"_id": 0})

    def query(
        self,
        action=None,
        subject=None,
        actor=None,
        run_id=None,
        limit=100,
        offset=0,
    ):
        q = {
            k: v
            for k, v in (
                ("action", action),
                ("subject", subject),
                ("actor", actor),
                ("run_id", run_id),
            )
            if v
        }
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        total = self.c.count_documents(q)
        cursor = self.c.find(q, {"_id": 0}).sort([("ts", -1), ("id", 1)]).skip(offset).limit(limit)
        return {
            "items": list(cursor),
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def mcp_metrics(self):
        rows = list(self.c.find({"action": {"$in": list(MCP_ACTIONS)}}))
        by = {a: 0 for a in MCP_ACTIONS}
        for r in rows:
            by[r["action"]] += 1
        errors = sum(1 for r in rows if (r.get("detail") or {}).get("is_error") is True)
        return {
            "total": len(rows),
            "by_action": by,
            "errors": errors,
            "error_rate": round(errors / len(rows), 4) if rows else 0.0,
        }


def _decode_settings_value(value: Any) -> Any:
    """Décode une valeur de paramètre agent lue dans Mongo (SCRUM-137).

    Parité avec le store SQLite (``core/agent_settings.AgentSettingsStore``),
    qui persiste ses valeurs JSON-encodées (``json.dumps``) et les relit via
    ``json.loads``. La migration ``scripts/migrate_sqlite_to_mongodb.py``
    copie les documents SQLite TELS QUELS : une valeur arrivée par ce chemin
    est donc une chaîne JSON (ex. ``'"openrouter"'`` avec guillemets) alors
    que les écritures runtime les stockent natives. Ce décodage tolérant
    normalise les TROIS états possibles :

      - chaîne JSON         → valeur décodée (``'"openrouter"'`` → ``openrouter``) ;
      - chaîne brute        → inchangée (``openrouter`` seul n'est pas du JSON valide) ;
      - valeur typée        → inchangée (int/float/bool/None conservés tels quels).
    """
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


class MongoAgentSettingsStore:
    """``AgentSettingsPort`` Mongo — symétrique du store SQLite.

    ``save_many`` persiste les valeurs JSON-encodées (MÊME format que le
    store SQLite) et ``get_all`` les décode via ``_decode_settings_value`` :
    les deux adaptateurs produisent le même contrat runtime et la migration
    SQLite→Mongo copie les documents sans divergence (SCRUM-137).
    """

    def __init__(self, provider=None, *args, **kwargs):
        db = provider if hasattr(provider, "collection") else get_mongo_provider()
        self.c = db.collection("agent_settings")

    def get_all(self):
        return {
            d["key"]: _decode_settings_value(d.get("value")) for d in self.c.find({}, {"_id": 0})
        }

    def save_many(self, values):
        from core.agent_settings import SETTING_KEYS

        filtered = {k: values[k] for k in SETTING_KEYS if k in values}
        for k, v in filtered.items():
            self.c.update_one(
                {"_id": k},
                {"$set": {"key": k, "value": json.dumps(v), "updated_at": time.time()}},
                upsert=True,
            )
        return filtered


class MongoJobStore(dict):
    """Dict-compatible job store with Mongo persistence."""

    def __init__(self, provider=None):
        super().__init__()
        provider = provider or get_mongo_provider()
        self.c = provider.collection("jobs")
        self.metrics = provider.collection("train_metrics")
        self.schedules = provider.collection("scheduled_jobs")
        self._load()

    def _load(self):
        for d in self.c.find({}, {"_id": 0}):
            self[d["job_id"]] = TrainJob(**d["payload"])

    def __setitem__(self, key, value):
        if not isinstance(value, TrainJob):
            raise TypeError("MongoJobStore accepts only TrainJob instances.")
        super().__setitem__(key, value)
        self.c.update_one(
            {"_id": str(key)},
            {
                "$set": {
                    "job_id": str(key),
                    "payload": value.model_dump(mode="json"),
                    "updated_at": time.time(),
                }
            },
            upsert=True,
        )

    def get(self, key, default=None):
        value = super().get(key)
        if value is not None:
            return value
        d = self.c.find_one({"_id": str(key)})
        if not d:
            return default
        value = TrainJob(**d["payload"])
        super().__setitem__(key, value)
        return value

    def list_jobs(self, status=None, kind=None, limit=100, offset=0):
        q = {}
        if status:
            q["payload.status"] = status
        if kind:
            q["payload.kind"] = kind
        total = self.c.count_documents(q)
        cursor = (
            self.c.find(q, {"_id": 0})
            .sort("payload.started_at", -1)
            .skip(max(0, offset))
            .limit(max(1, limit))
        )
        return [TrainJob(**d["payload"]) for d in cursor], total

    def save_epoch_metrics(self, job_id, records):
        for r in records:
            self.metrics.update_one(
                {"job_id": job_id, "epoch": int(r.get("epoch", 0))},
                {"$set": {**r, "job_id": job_id, "updated_at": time.time()}},
                upsert=True,
            )

    def get_job_metrics(self, job_id):
        cursor = self.metrics.find({"job_id": job_id}, {"_id": 0}).sort("epoch", 1)
        return list(cursor)

    def save_schedule(self, schedule):
        self.schedules.update_one({"_id": schedule["schedule_id"]}, {"$set": schedule}, upsert=True)

    def get_schedules(self):
        return list(self.schedules.find({}, {"_id": 0}).sort("updated_at", 1))

    def get_schedule(self, schedule_id):
        return self.schedules.find_one({"_id": schedule_id}, {"_id": 0})

    def delete_schedule(self, schedule_id):
        return bool(self.schedules.delete_one({"_id": schedule_id}).deleted_count)

    def remove_job(self, job_id):
        self.metrics.delete_many({"job_id": job_id})
        self.pop(job_id, None)
        return bool(self.c.delete_one({"_id": job_id}).deleted_count)

    def update_job_timestamp(self, job_id, timestamp):
        r = self.c.update_one({"_id": job_id}, {"$set": {"updated_at": timestamp}})
        return bool(r.matched_count)

    def cleanup_old_jobs(self, max_age_days=30, dry_run=False):
        query = {
            "updated_at": {"$lt": time.time() - max_age_days * 86400},
            "payload.status": {"$in": ["completed", "failed", "cancelled"]},
        }
        ids = [d["job_id"] for d in self.c.find(query, {"job_id": 1})]
        if not dry_run:
            for i in ids:
                self.remove_job(i)
        return {"deleted": 0 if dry_run else len(ids), "job_ids": ids}


class MongoMCPClientStore:
    def __init__(self, provider=None):
        self.c = (provider or get_mongo_provider()).collection("mcp_clients")

    @staticmethod
    def _hash_secret(secret):
        return hashlib.sha256(secret.encode()).hexdigest()

    def register(self, client_id, secret, scope):
        from core.mcp_client_store import MCPClientAlreadyExistsError

        if not client_id or not secret:
            raise ValueError("client_id et secret sont obligatoires")
        if self.c.find_one({"_id": client_id}):
            raise MCPClientAlreadyExistsError(client_id)
        now = _utcnow()
        d = {
            "_id": client_id,
            "client_id": client_id,
            "secret_hash": self._hash_secret(secret),
            "scope": scope.model_dump(),
            "call_count": 0,
            "error_count": 0,
            "scope_usage": {},
            "created_at": now,
            "updated_at": now,
            "revoked": False,
            "revoked_at": None,
            "revoked_reason": "",
        }
        self.c.insert_one(d)
        return self._public(d)

    def _public(self, d):
        s = MCPSecurityScope.model_validate(d["scope"])
        return {
            "client_id": d["client_id"],
            **s.model_dump(),
            "revoked": d["revoked"],
            "revoked_at": d["revoked_at"],
            "revoked_reason": d["revoked_reason"],
            "call_count": d["call_count"],
            "error_count": d["error_count"],
            "scope_usage": d["scope_usage"],
            "created_at": d["created_at"],
            "updated_at": d["updated_at"],
        }

    def get_by_client_id(self, cid):
        d = self.c.find_one({"_id": cid})
        return self._public(d) if d else None

    def list(self):
        return [self._public(d) for d in self.c.find({})]

    def revoke(self, cid, reason):
        from core.mcp_client_store import MCPClientNotFoundError, MCPClientRevokedError

        d = self.c.find_one({"_id": cid})
        if not d:
            raise MCPClientNotFoundError(cid)
        if d["revoked"]:
            raise MCPClientRevokedError(cid)
        self.c.update_one(
            {"_id": cid},
            {
                "$set": {
                    "revoked": True,
                    "revoked_at": _utcnow(),
                    "revoked_reason": reason or "",
                    "updated_at": _utcnow(),
                }
            },
        )
        return self.get_by_client_id(cid)

    def get_scope(self, cid):
        from core.mcp_client_store import MCPClientNotFoundError

        d = self.c.find_one({"_id": cid})
        if not d:
            raise MCPClientNotFoundError(cid)
        return MCPSecurityScope.model_validate(d["scope"])

    def authenticate(self, cid, secret):
        d = self.c.find_one({"_id": cid})
        if not d:
            return False
        return bool(not d["revoked"] and d["secret_hash"] == self._hash_secret(secret))

    def record_call(self, cid, tool_name, success):
        self.c.update_one(
            {"_id": cid},
            {
                "$inc": {
                    "call_count": 1,
                    "error_count": 0 if success else 1,
                    f"scope_usage.{tool_name}": 1,
                },
                "$set": {"updated_at": _utcnow()},
            },
        )

    def metrics(self, cid):
        d = self.c.find_one({"_id": cid})
        calls = int(d.get("call_count", 0)) if d else 0
        errors = int(d.get("error_count", 0)) if d else 0
        return {
            "call_count": calls,
            "error_count": errors,
            "error_rate": round(errors / calls, 4) if calls else 0.0,
            "scope_usage": d.get("scope_usage", {}) if d else {},
        }

    def count(self):
        return self.c.count_documents({})

    def count_active(self):
        return self.c.count_documents({"revoked": False})

    def count_revoked(self):
        return self.c.count_documents({"revoked": True})


class MongoToolAuditStore:
    """Compatibility adapter for ``ia.agent.audit``'s tool-call contract."""

    def __init__(self, provider=None):
        self.c = (provider or get_mongo_provider()).collection("tool_audit")

    def log_tool_call(self, tool_name, args, result=None, duration_ms=0.0,
                      success=True, error_message=None, job_id=None):
        d = {
            "_id": uuid.uuid4().hex,
            "job_id": job_id,
            "tool_name": tool_name,
            "args": args,
            "result": result if success else None,
            "duration_ms": duration_ms,
            "success": bool(success),
            "error_message": error_message,
            "created_at": time.time(),
        }
        self.c.insert_one(d)
        return d["_id"]

    def get_trail(self, job_id=None, tool_name=None, limit=100, since=None):
        q = {}
        if job_id:
            q["job_id"] = job_id
        if tool_name:
            q["tool_name"] = tool_name
        if since:
            q["created_at"] = {"$gte": since}
        rows = self.c.find(q, {"_id": 0}).sort("created_at", -1).limit(max(1, int(limit)))
        return [{**d, "tool": d.pop("tool_name", "")} for d in rows]

    def cleanup_old_entries(self, days=30):
        result = self.c.delete_many({"created_at": {"$lt": time.time() - days * 86400}})
        return int(result.deleted_count)

    def get_stats(self):
        total = self.c.count_documents({})
        errors = self.c.count_documents({"success": False})
        return {
            "total_entries": total,
            "total_errors": errors,
            "error_rate": round(errors / total, 4) if total else 0.0,
            "unique_tools": len(self.c.distinct("tool_name")),
        }


class MongoFeedbackStore:
    def __init__(self, provider=None):
        self.c = (provider or get_mongo_provider()).collection("copilot_feedback")

    def record(self, tool, accepted, kind="tool", session_id="", suggestion=None):
        d = {
            "_id": uuid.uuid4().hex,
            "id": "",
            "created_at": _utcnow(),
            "session_id": session_id or "",
            "kind": kind,
            "suggestion": suggestion or {},
            "tool": tool or "",
            "accepted": bool(accepted),
        }
        d["id"] = d["_id"]
        self.c.insert_one(d)
        d.pop("_id", None)
        return d

    def stats(self):
        out = {}
        for tool in self.c.distinct("tool"):
            acc = self.c.count_documents({"tool": tool, "accepted": True})
            rej = self.c.count_documents({"tool": tool, "accepted": False})
            total = acc + rej
            out[tool] = {"accepts": acc, "rejects": rej,
                         "accept_rate": round(acc / total, 3) if total else 0.0}
        return out

    def boost(self, tool):
        s = self.stats().get(tool or "", {"accepts": 0, "rejects": 0})
        acc, rej = s["accepts"], s["rejects"]
        if not acc and not rej:
            return 0.0
        return -0.15 if rej > acc else min(0.3, 0.1 * acc)

    def count(self):
        return int(self.c.count_documents({}))


class MongoServiceAccountStore:
    """Service-account store preserving the public SQLite API."""

    def __init__(self, provider=None):
        self.c = (provider or get_mongo_provider()).collection("service_accounts")
        self.revoked = (provider or get_mongo_provider()).collection("revoked_tokens")

    def create_account(self, *, name, role="read", scopes=None):
        import secrets

        from app.infrastructure.security.service_accounts import (
            _audit,
            _hash_secret,
        )
        if not name or len(name.strip()) < 3 or role not in ("admin", "read"):
            raise ValueError("nom ou rôle de service account invalide")
        name = name.strip()
        if self.c.find_one({"name": name}):
            raise ValueError(f"nom de service account déjà pris : {name}")
        secret = secrets.token_urlsafe(32)
        d = {"_id": str(uuid.uuid4()), "name": name, "role": role,
             "scopes": list(scopes or []), "secret_hash": _hash_secret(secret),
             "enabled": True, "created_at": _utcnow(), "last_used_at": ""}
        self.c.insert_one(d)
        _audit("service_account_created", name, {"account_id": d["_id"], "role": role,
                                                  "scopes": d["scopes"]})
        return {"id": d["_id"], "name": name, "role": role, "scopes": d["scopes"],
                "client_secret": secret}

    def register_account(self, *, email, password, role="read", scopes=None):
        from app.infrastructure.security.service_accounts import (
            MAX_PASSWORD_LENGTH,
            MIN_PASSWORD_LENGTH,
            EmailAlreadyTakenError,
            RegistrationError,
        )
        email = (email or "").strip().lower()
        if "@" not in email:
            raise RegistrationError("adresse email invalide")
        if not (MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH):
            raise RegistrationError("mot de passe de longueur invalide")
        if self.c.find_one({"name": email}):
            raise EmailAlreadyTakenError(f"un compte existe déjà avec cet email : {email}")
        from app.infrastructure.security.service_accounts import _audit, _hash_secret
        d = {"_id": str(uuid.uuid4()), "name": email, "role": role,
             "scopes": list(scopes or []), "secret_hash": _hash_secret(password),
             "enabled": True, "created_at": _utcnow(), "last_used_at": ""}
        self.c.insert_one(d)
        _audit("service_account_registered", email, {"account_id": d["_id"], "role": role})
        return {"id": d["_id"], "email": email, "role": role}

    def list_accounts(self):
        out = []
        for d in self.c.find({}):
            out.append({
                "id": d["_id"],
                "name": d.get("name", ""),
                "role": d.get("role", ""),
                "scopes": d.get("scopes", []),
                "enabled": bool(d.get("enabled")),
                "created_at": d.get("created_at", ""),
                "last_used_at": d.get("last_used_at", ""),
            })
        return out

    def _get_by_client_id(self, account_id):
        return self.c.find_one({
            "$or": [
                {"_id": str(account_id)},
                {"name": str(account_id).strip().lower()},
            ]
        })

    def issue_token(self, *, client_id, client_secret, jwt_secret, ttl_seconds=900):
        import secrets

        from app.infrastructure.security.service_accounts import _audit, _hash_secret
        d = self._get_by_client_id(client_id)
        if not d or not d.get("enabled") or not secrets.compare_digest(
            d["secret_hash"], _hash_secret(client_secret)
        ):
            _audit("service_account_authenticated", client_id, {"ok": False})
            raise PermissionError("client_id ou secret invalide")
        from app.domain.tokens import create_access_token
        token = create_access_token(subject=d["name"], secret=jwt_secret, role=d["role"],
                                    scopes=d.get("scopes", []), ttl_seconds=ttl_seconds)
        self.c.update_one({"_id": d["_id"]}, {"$set": {"last_used_at": _utcnow()}})
        return {"token": token, "role": d["role"]}

    def revoke_account(self, account_id):
        return bool(self.c.delete_one({"_id": str(account_id)}).deleted_count)

    def revoke_token(self, claims):
        if not claims.jti:
            return False
        self.revoked.update_one(
            {"_id": claims.jti},
            {"$set": {"expires_at": claims.expires_at}},
            upsert=True,
        )
        return True

    def is_token_revoked(self, jti):
        return bool(jti and self.revoked.find_one({"_id": str(jti)}))
