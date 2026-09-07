"""Piste d'audit persistante pour les actions de l'agent.

Enregistre chaque appel d'outil dans une base SQLite requettable par
job_id, tool_name, ou plage temporelle. Utilise pour :
    - Debug et replay de sessions
    - Conformite et traçabilite
    - Analyse post-mortem des erreurs
"""

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("thinktuning.agent.audit")

AUDIT_DB_PATH = os.getenv(
    "AGENT_AUDIT_PATH",
    os.path.join("experiments", "agent_audit.db"),
)
RETENTION_DAYS = int(os.getenv("AGENT_AUDIT_RETENTION_DAYS", "30"))


def _safe_json(obj: Any) -> Optional[str]:
    """Serialise en JSON de maniere sure."""
    if obj is None:
        return None
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


class AuditStore:
    """Store SQLite pour la piste d'audit."""

    def __init__(self, path: str = AUDIT_DB_PATH) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._ensure_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30.0)

    def _ensure_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS audit_log (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT,
                        tool_name TEXT NOT NULL,
                        args_json TEXT,
                        result_json TEXT,
                        duration_ms REAL,
                        success INTEGER NOT NULL DEFAULT 1,
                        error_message TEXT,
                        created_at REAL NOT NULL
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_audit_job_id
                    ON audit_log(job_id)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_audit_tool_name
                    ON audit_log(tool_name)
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_audit_created_at
                    ON audit_log(created_at)
                """)
                conn.commit()
            finally:
                conn.close()

    def log_tool_call(
        self,
        tool_name: str,
        args: Dict[str, Any],
        result: Any = None,
        duration_ms: float = 0.0,
        success: bool = True,
        error_message: Optional[str] = None,
        job_id: Optional[str] = None,
    ) -> int:
        """Enregistre un appel d'outil."""
        args_json = _safe_json(args)
        result_json = _safe_json(result) if success else None

        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO audit_log
                    (job_id, tool_name, args_json, result_json,
                     duration_ms, success, error_message, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id, tool_name, args_json, result_json,
                        duration_ms, 1 if success else 0,
                        error_message, time.time(),
                    ),
                )
                conn.commit()
                return cursor.lastrowid or 0
            finally:
                conn.close()

    def get_trail(
        self,
        job_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        limit: int = 100,
        since: Optional[float] = None,
    ) -> List[Dict[str, Any]]:
        """Requete la piste d'audit.

        Retourne des dicts avec les cles ``tool`` (alias de tool_name),
        ``job_id``, ``result`` (JSON parse), ``success``, etc.
        """
        query = """
            SELECT id, job_id, tool_name AS tool, args_json AS args,
                   result_json AS result, duration_ms, success,
                   error_message, created_at
            FROM audit_log WHERE 1=1
        """
        params: list = []

        if job_id:
            query += " AND job_id = ?"
            params.append(job_id)
        if tool_name:
            query += " AND tool_name = ?"
            params.append(tool_name)
        if since:
            query += " AND created_at >= ?"
            params.append(since)

        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._lock:
            conn = self._connect()
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(query, params).fetchall()
                results = [dict(row) for row in rows]
                for r in results:
                    if r.get("result"):
                        try:
                            r["result"] = json.loads(r["result"])
                        except (json.JSONDecodeError, TypeError):
                            pass
                return results
            finally:
                conn.close()

    def cleanup_old_entries(self, days: int = RETENTION_DAYS) -> int:
        """Supprime les entrees plus anciennes que `days` jours."""
        cutoff = time.time() - (days * 86400)
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    "DELETE FROM audit_log WHERE created_at < ?",
                    (cutoff,),
                )
                conn.commit()
                deleted = cursor.rowcount or 0
                if deleted > 0:
                    logger.info("audit: cleaned up %d old entries", deleted)
                return deleted
            finally:
                conn.close()

    def get_stats(self) -> Dict[str, Any]:
        """Statistiques globales de la piste d'audit."""
        with self._lock:
            conn = self._connect()
            try:
                total = conn.execute(
                    "SELECT COUNT(*) FROM audit_log"
                ).fetchone()[0]
                errors = conn.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE success = 0"
                ).fetchone()[0]
                tools = conn.execute(
                    "SELECT DISTINCT tool_name FROM audit_log"
                ).fetchall()
                return {
                    "total_entries": total,
                    "total_errors": errors,
                    "error_rate": round(errors / total, 4) if total else 0.0,
                    "unique_tools": len(tools),
                }
            finally:
                conn.close()


# ============================================================
# Singleton global
# ============================================================

_store: Optional[AuditStore] = None
_store_lock = threading.Lock()


def get_audit_store() -> AuditStore:
    """Retourne l'instance singleton du AuditStore."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = AuditStore()
    return _store


def reset_audit_store() -> None:
    """Reinitialise le singleton (pour les tests)."""
    global _store
    with _store_lock:
        _store = None


# ============================================================
# Fonctions pratiques
# ============================================================

def log_tool_call(
    tool_name: Optional[str] = None,
    args: Optional[Dict[str, Any]] = None,
    result: Any = None,
    duration_ms: float = 0.0,
    success: bool = True,
    error_message: Optional[str] = None,
    job_id: Optional[str] = None,
    *,
    tool: Optional[str] = None,
    **kwargs: Any,
) -> int:
    """Enregistre un appel d'outil sur le store global.

    ``tool`` est accepté comme alias (mot-clé uniquement) de ``tool_name``::

        log_tool_call(tool="read_file", args={...}, result="ok", success=True)
        log_tool_call("read_file", {...}, "ok", 1.0, True, job_id="run-1")

    Les paramètres inconnus passés dans ``kwargs`` sont ignorés (tolérant au
    renforcement progressif des événements) ; ``job_id`` y est néanmoins
    extrait au cas où il serait passé via ``**kwargs``.
    """
    if job_id is None and kwargs:
        job_id = kwargs.get("job_id")
    actual_tool = tool_name or tool
    if args is None:
        args = {}
    if actual_tool is None:
        raise ValueError("tool_name (ou tool) est requis")
    return get_audit_store().log_tool_call(
        tool_name=actual_tool,
        args=args,
        result=result,
        duration_ms=duration_ms,
        success=success,
        error_message=error_message,
        job_id=job_id,
    )


def get_audit_trail(
    job_id: Optional[str] = None,
    tool_name: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Requete la piste d'audit sur le store global."""
    return get_audit_store().get_trail(
        job_id=job_id,
        tool_name=tool_name,
        limit=limit,
    )


def get_tool_history(tool_name: str, limit: int = 50) -> List[Dict[str, Any]]:
    """Recupere l'historique des appels pour un outil specifique."""
    return get_audit_store().get_trail(tool_name=tool_name, limit=limit)


def clear_audit_log(job_id: Optional[str] = None) -> None:
    """Vide le journal d'audit (ou seulement les entrees d'un job)."""
    store = get_audit_store()
    with store._lock:
        conn = store._connect()
        try:
            if job_id:
                conn.execute("DELETE FROM audit_log WHERE job_id = ?", (job_id,))
            else:
                conn.execute("DELETE FROM audit_log")
            conn.commit()
        finally:
            conn.close()


_audit_db_path_override: Optional[str] = None


def set_audit_db_path(path: str) -> None:
    """Definit le chemin de la base de donnees d'audit (pour les tests)."""
    global _audit_db_path_override, _store
    _audit_db_path_override = path
    with _store_lock:
        _store = None


def get_audit_db_path() -> str:
    """Retourne le chemin actuel de la base de donnees d'audit."""
    return _audit_db_path_override if _audit_db_path_override else AUDIT_DB_PATH