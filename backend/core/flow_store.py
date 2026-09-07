# project/core/flow_store.py

"""Journal des sessions d'orchestration multi-agents (« Flow Map ») — SQLite.

Chaque invocation de ``POST /api/agent/multi/ask/stream`` crée une ligne
``running`` (session de flux) puis est clôturée en fin d'exécution. Tous les
événements SSE émis par l'orchestrateur (``agent.plan``, ``agent.worker.*``,
``agent.done``…) y sont enregistrés, horodatés en millisecondes **relatives**
au début de la session : c'est exactement la timeline rejouable qu'affiche le
dashboard (« Agent Flow Map » — modes Replay et Heatmap).

Mêmes conventions que ``core/run_store.py`` / ``core/approval_store.py`` :
    - base SQLite dédiée (``experiments/agent_flows.db``, surchargeable via
      ``AGENT_FLOW_PATH`` pour isoler les tests) ;
    - store thread-safe (le worker SSE tourne dans un thread dédié).
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import UTC, datetime

AGENT_FLOW_PATH = os.getenv("AGENT_FLOW_PATH", os.path.join("experiments", "agent_flows.db"))

# Statuts d'une session de flux. ``running`` pendant l'exécution des workers ;
# ``completed`` / ``error`` pour les issues finales. ``awaiting_approval`` /
# ``rejected`` conservés pour rester aligné sur la sémantique des runs.
RUNNING = "running"
COMPLETED = "completed"
AWAITING_APPROVAL = "awaiting_approval"
REJECTED = "rejected"
ERROR = "error"

STATUSES = (RUNNING, COMPLETED, AWAITING_APPROVAL, REJECTED, ERROR)

_SELECT_COLUMNS = (
    "id, prompt, model, status, answer_summary, error, "
    "events_json, created_at, finished_at"
)


def _utcnow_iso() -> str:
    """Horodatage ISO 8601 UTC (millisecondes) — stable, triable."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class FlowStore:
    """Journal des sessions multi-agents au-dessus d'une table SQLite (thread-safe)."""

    def __init__(self, path: str = AGENT_FLOW_PATH):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._ensure_db()

    # --- Infra ----------------------------------------------------------------

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30.0)

    def _ensure_db(self):
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_flows (
                id             TEXT PRIMARY KEY,
                prompt         TEXT NOT NULL DEFAULT '',
                model          TEXT NOT NULL DEFAULT '',
                status         TEXT NOT NULL DEFAULT 'running',
                answer_summary TEXT NOT NULL DEFAULT '',
                error          TEXT,
                events_json    TEXT NOT NULL DEFAULT '[]',
                created_at     TEXT NOT NULL,
                finished_at    TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _row_to_dict(row):
        if row is None:
            return None
        keys = ["id", "prompt", "model", "status", "answer_summary", "error",
                "events_json", "created_at", "finished_at"]
        data = dict(zip(keys, row, strict=True))
        try:
            data["events"] = json.loads(data.pop("events_json") or "[]")
        except ValueError:
            data["events"] = []
        return data

    def _get_raw(self, conn, flow_id):
        return conn.execute(
            f"SELECT {_SELECT_COLUMNS} FROM agent_flows WHERE id = ?", (str(flow_id),)
        ).fetchone()
# --- Cycle de vie ------------------------------------------------------------

    def start_flow(
        self,
        prompt: str,
        model: str = "",
        source: str = "api",
    ) -> dict:
        """Crée une session ``running`` et retourne la ligne complète."""
        flow_id = uuid.uuid4().hex[:12]
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO agent_flows (
                            id, prompt, model, status, answer_summary,
                            error, events_json, created_at, finished_at
                        ) VALUES (?, ?, ?, ?, '', NULL, '[]', ?, NULL)
                        """,
                        (flow_id, prompt or "", model or "", RUNNING, _utcnow_iso()),
                    )
            finally:
                conn.close()
        return self.get(flow_id) or {"id": flow_id}

    def append_event(self, flow_id: str, event: str, data: dict, at_ms: float) -> None:
        """Ajoute un événement SSE à la timeline JSON de la session.

        ``event`` = nom (``agent.plan``…), ``data`` = charge utile brute,
        ``at_ms`` = millisecondes relatives au début de la session. Ne lève
        jamais : la persistance ne doit pas bloquer le flux d'exécution.
        """
        entry = {"event": event, "data": data, "at_ms": round(at_ms, 2)}
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT events_json FROM agent_flows WHERE id = ?", (str(flow_id),)
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
                        "UPDATE agent_flows SET events_json = ? WHERE id = ?",
                        (json.dumps(events, ensure_ascii=False), str(flow_id)),
                    )
            finally:
                conn.close()

    def finish_flow(
        self,
        flow_id: str,
        status: str,
        answer_summary: str = "",
        error: str | None = None,
    ) -> dict | None:
        """Clôture une session : statut final, résumé de réponse ou erreur."""
        if status not in STATUSES:
            raise ValueError(f"Statut de session inconnu : '{status}'")
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT status FROM agent_flows WHERE id = ?", (str(flow_id),)
                ).fetchone()
                if existing is None:
                    return None
                with conn:
                    conn.execute(
                        """
                        UPDATE agent_flows
                        SET status = ?, answer_summary = ?, error = ?, finished_at = ?
                        WHERE id = ?
                        """,
                        (status, answer_summary or "", error, _utcnow_iso(), str(flow_id)),
                    )
                row = conn.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM agent_flows WHERE id = ?",
                    (str(flow_id),),
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_dict(row)

    # --- Consultation ---------------------------------------------------------------

    def get(self, flow_id: str) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    f"SELECT {_SELECT_COLUMNS} FROM agent_flows WHERE id = ?",
                    (str(flow_id),),
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_dict(row)

    def list(
        self,
        limit: int = 50,
        status: str | None = None,
    ) -> list[dict]:
        """Sessions les plus récentes d'abord. Filtre optionnel par statut.

        Ne renvoie PAS les événements (volume) : seul ``get`` les détaille. Les
        compteurs d'outils / d'agents sont fournis pour la liste du dashboard.
        """
        limit = max(1, min(int(limit), 200))
        summaries = []
        with self._lock:
            conn = self._connect()
            try:
                sql = (
                    "SELECT id, prompt, model, status, answer_summary, error, "
                    "events_json, created_at, finished_at FROM agent_flows"
                )
                conditions, params = [], []
                if status:
                    conditions.append("status = ?")
                    params.append(status)
                if conditions:
                    sql += " WHERE " + " AND ".join(conditions)
                sql += " ORDER BY created_at DESC LIMIT ?"
                params.append(limit)
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        for row in rows:
            item = self._row_to_dict(row)
            if item is None:
                continue
            events = item.get("events") or []
            # Compteurs utiles au dashboard sans transporter toute la timeline.
            # ``core.tool`` : événements du noyau v2 (/ask/core/stream).
            # 1 appel d'outil = 1 : seuls les « tool_start » sont comptés.
            # En mode noyau v2, un appel émet deux événements core.tool
            # (tool_start + tool_result) : sans ce filtre le compteur était
            # doublé par rapport au mode multi-agents.
            item["tool_calls"] = sum(
                1
                for e in events
                if e.get("event") in ("agent.worker.tool", "core.tool")
                and (e.get("data") or {}).get("event") != "tool_result"
            )
            roles: set[str] = set()
            for e in events:
                if e.get("event") in ("agent.worker.start", "core.start") and e.get("data"):
                    r = e["data"].get("role")
                    if r:
                        roles.add(str(r))
            item["agents"] = sorted(roles)
            item.pop("events", None)
            summaries.append(item)
        return summaries

    def delete(self, flow_id: str) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "DELETE FROM agent_flows WHERE id = ?", (str(flow_id),)
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()


# --- Store partagé (lazy, surchargeable en tests) ---------------------------------------

_store: FlowStore | None = None
_store_lock = threading.Lock()


def get_flow_store() -> FlowStore:
    """Store partagé de l'application (instance unique paresseuse)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = FlowStore(AGENT_FLOW_PATH)
        return _store


def reset_flow_store(path: str | None = None) -> FlowStore:
    """Remplace le store partagé par une base neuve (isolation des tests)."""
    global _store
    with _store_lock:
        _store = FlowStore(path or AGENT_FLOW_PATH)
        return _store
