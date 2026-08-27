# project/core/run_store.py

"""Journal des exécutions de l'agent IA (« runs ») — SQLite.

Chaque invocation de l'agent via l'API (POST /api/agent/ask, POST
/api/agent/ask/stream) crée une ligne ``running`` puis est clôturée à la fin :
statut final, résumé de la réponse, erreur éventuelle et TRAÇABILITÉ de la
chaîne d'outils appelée (un événement JSON par outil : arguments tronqués,
statut ok/error, aperçu du résultat, durée).

Mêmes conventions que ``core/approval_store.py`` :
    - base SQLite dédiée (experiments/agent_runs.db, surchargeable via
      AGENT_RUN_PATH pour isoler les tests) ;
    - store thread-safe (l'API FastAPI appelle depuis plusieurs threads).
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

AGENT_RUN_PATH = os.getenv("AGENT_RUN_PATH", os.path.join("experiments", "agent_runs.db"))

# Statuts d'un run. ``running`` pendant l'exécution ; ``awaiting_approval``
# quand le gate exige une décision humaine ; ``rejected`` si la policy a
# bloqué ; ``completed`` / ``error`` pour les issues finales.
RUNNING = "running"
COMPLETED = "completed"
AWAITING_APPROVAL = "awaiting_approval"
REJECTED = "rejected"
ERROR = "error"

STATUSES = (RUNNING, COMPLETED, AWAITING_APPROVAL, REJECTED, ERROR)

_SELECT_COLUMNS = (
    "id, prompt, model, source, status, answer_summary, error, "
    "tools_json, created_at, finished_at"
)


def _utcnow_iso() -> str:
    """Horodatage ISO 8601 UTC (millisecondes) — stable, triable."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class RunStore:
    """Journal des runs de l'agent au-dessus d'une table SQLite (thread-safe)."""

    def __init__(self, path: str = AGENT_RUN_PATH):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._ensure_db()

    # --- Infra -----------------------------------------------------------------

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30.0)

    def _ensure_db(self):
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_runs (
                id             TEXT PRIMARY KEY,
                prompt         TEXT NOT NULL DEFAULT '',
                model          TEXT NOT NULL DEFAULT '',
                source         TEXT NOT NULL DEFAULT 'api',
                status         TEXT NOT NULL DEFAULT 'running',
                answer_summary TEXT NOT NULL DEFAULT '',
                error          TEXT,
                tools_json     TEXT NOT NULL DEFAULT '[]',
                created_at     TEXT NOT NULL,
                finished_at    TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _row_to_dict(row) -> dict | None:
        if row is None:
            return None
        keys = [
            "id",
            "prompt",
            "model",
            "source",
            "status",
            "answer_summary",
            "error",
            "tools_json",
            "created_at",
            "finished_at",
        ]
        data = dict(zip(keys, row))
        try:
            data["tools"] = json.loads(data.pop("tools_json") or "[]")
        except ValueError:
            data["tools"] = []
        return data

    # --- Cycle de vie ------------------------------------------------------------

    def start_run(
        self,
        prompt: str,
        model: str = "",
        source: str = "api",
    ) -> dict:
        """Crée un run ``running`` et retourne la ligne complète."""
        run_id = uuid.uuid4().hex[:12]
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO agent_runs (
                            id, prompt, model, source, status,
                            answer_summary, error, tools_json, created_at, finished_at
                        ) VALUES (?, ?, ?, ?, ?, '', NULL, '[]', ?, NULL)
                        """,
                        (run_id, prompt or "", model or "", source, RUNNING, _utcnow_iso()),
                    )
            finally:
                conn.close()
        return self.get(run_id) or {"id": run_id}

    def append_tool_event(self, run_id: str, event: dict) -> None:
        """Ajoute un événement d'outil au journal JSON du run.

        ``event`` est le dict émis par AgentCore (tool_start / tool_result) ;
        il est stocké tel quel (horodaté à la réception) sans jamais bloquer
        le flux d'exécution.
        """
        entry = {**event, "at": _utcnow_iso()}
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT tools_json FROM agent_runs WHERE id = ?", (str(run_id),)
                ).fetchone()
                if row is None:
                    return
                try:
                    events = json.loads(row[0] or "[]")
                except ValueError:
                    events = []
                events.append(entry)
                with conn:
                    conn.execute(
                        "UPDATE agent_runs SET tools_json = ? WHERE id = ?",
                        (json.dumps(events, ensure_ascii=False), str(run_id)),
                    )
            finally:
                conn.close()

    def finish_run(
        self,
        run_id: str,
        status: str,
        answer_summary: str = "",
        error: str | None = None,
    ) -> dict | None:
        """Clôture un run : statut final, résumé de réponse ou erreur."""
        if status not in STATUSES:
            raise ValueError(f"Statut de run inconnu : '{status}'")
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT status FROM agent_runs WHERE id = ?", (str(run_id),)
                ).fetchone()
                if existing is None:
                    return None
                with conn:
                    conn.execute(
                        """
                        UPDATE agent_runs
                        SET status = ?, answer_summary = ?, error = ?, finished_at = ?
                        WHERE id = ?
                        """,
                        (status, answer_summary or "", error, _utcnow_iso(), str(run_id)),
                    )
                row = conn.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM agent_runs WHERE id = ?",
                    (str(run_id),),
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_dict(row)

    # --- Consultation ---------------------------------------------------------------

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM agent_runs WHERE id = ?",
                    (str(run_id),),
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_dict(row)

    def list(
        self,
        limit: int = 50,
        status: str | None = None,
        tool: str | None = None,
    ) -> list[dict]:
        """Runs les plus récents d'abord. Filtres optionnels :

        ``status`` sur la colonne dédiée ; ``tool`` en recherche LIKE dans le
        journal JSON des outils (filtre opportuniste, volontairement non indexé).
        """
        limit = max(1, min(int(limit), 200))
        with self._lock:
            conn = self._connect()
            try:
                sql = f"SELECT {_SELECT_COLUMNS} FROM agent_runs"
                conditions, params = [], []
                if status:
                    conditions.append("status = ?")
                    params.append(status)
                if tool:
                    conditions.append("tools_json LIKE ?")
                    params.append(f'%"tool": "{tool}"%')
                if conditions:
                    sql += " WHERE " + " AND ".join(conditions)
                sql += " ORDER BY created_at DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        return [d for d in (self._row_to_dict(row) for row in rows) if d is not None]


# --- Store partagé (lazy, surchargeable en tests) ------------------------------------

_store: RunStore | None = None
_store_lock = threading.Lock()


def get_run_store() -> RunStore:
    """Store partagé de l'application (instance unique paresseuse)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = RunStore(AGENT_RUN_PATH)
        return _store


def reset_run_store(path: str | None = None) -> RunStore:
    """Remplace le store partagé par une base neuve (isolation des tests)."""
    global _store
    with _store_lock:
        _store = RunStore(path or AGENT_RUN_PATH)
        return _store
