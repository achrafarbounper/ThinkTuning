"""Apprentissage des interactions utilisateur (Phase D — flag AGENT_COPILOT).

Enregistre l'issue des suggestions proposées à l'utilisateur (acceptée /
refusée) dans une base SQLite dédiée, puis en dérive un **boost de
pertinence** appliqué au re-classement des suggestions suivantes : un outil
souvent accepté monte dans la liste, un outil souvent refusé descend.

Mêmes conventions que les autres stores du projet (``core/audit_store.py``…):

    - base SQLite dédiée (experiments/agent_copilot.db, surchargeable via
      AGENT_COPILOT_PATH pour isoler les tests) ;
    - store thread-safe ;
    - singleton paresseux ``get_feedback_store()`` + ``reset_feedback_store()``.
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

AGENT_COPILOT_PATH = os.getenv(
    "AGENT_COPILOT_PATH", os.path.join("experiments", "agent_copilot.db")
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class FeedbackStore:
    """Acceptations / refus de suggestions (SQLite, thread-safe)."""

    def __init__(self, path: str = AGENT_COPILOT_PATH):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._ensure_db()

    # --- Infra ---------------------------------------------------------------

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30.0)

    def _ensure_db(self):
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS copilot_feedback (
                id              TEXT PRIMARY KEY,
                created_at      TEXT NOT NULL,
                session_id      TEXT NOT NULL DEFAULT '',
                kind            TEXT NOT NULL DEFAULT 'tool',
                suggestion_json TEXT NOT NULL DEFAULT '{}',
                tool            TEXT NOT NULL DEFAULT '',
                accepted        INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_copilot_tool "
            "ON copilot_feedback(tool)"
        )
        conn.commit()
        conn.close()

    # --- Écriture ------------------------------------------------------------

    def record(
        self,
        tool: str,
        accepted: bool,
        kind: str = "tool",
        session_id: str = "",
        suggestion: dict | None = None,
    ) -> dict:
        """Trace l'issue d'une suggestion et renvoie la ligne créée."""
        entry = {
            "id": uuid.uuid4().hex,
            "created_at": _utcnow_iso(),
            "session_id": session_id or "",
            "kind": kind,
            "suggestion_json": json.dumps(suggestion or {}, ensure_ascii=False),
            "tool": tool or "",
            "accepted": 1 if accepted else 0,
        }
        with self._lock:
            conn = self._connect()
            conn.execute(
                "INSERT INTO copilot_feedback (id, created_at, session_id, kind,"
                " suggestion_json, tool, accepted) VALUES (?,?,?,?,?,?,?)",
                (
                    entry["id"], entry["created_at"], entry["session_id"],
                    entry["kind"], entry["suggestion_json"], entry["tool"],
                    entry["accepted"],
                ),
            )
            conn.commit()
            conn.close()
        return entry

    # --- Lecture / stats -----------------------------------------------------

    def _counts(self, conn) -> dict[str, tuple[int, int]]:
        rows = conn.execute(
            "SELECT tool,"
            " SUM(CASE WHEN accepted=1 THEN 1 ELSE 0 END) AS acc,"
            " SUM(CASE WHEN accepted=0 THEN 1 ELSE 0 END) AS rej"
            " FROM copilot_feedback GROUP BY tool"
        ).fetchall()
        return {r[0]: (int(r[1] or 0), int(r[2] or 0)) for r in rows}

    def stats(self) -> dict[str, dict]:
        """Par outil : ``accepts``, ``rejects``, ``accept_rate`` (0..1)."""
        with self._lock:
            conn = self._connect()
            counts = self._counts(conn)
            conn.close()
        out: dict[str, dict] = {}
        for tool, (acc, rej) in counts.items():
            total = acc + rej
            out[tool] = {
                "accepts": acc,
                "rejects": rej,
                "accept_rate": round(acc / total, 3) if total else 0.0,
            }
        return out

    def boost(self, tool: str) -> float:
        """Ajustement de score (peut être négatif) pour un outil.

        Déterministe : +0.1 par acceptation (plafonné à +0.3), −0.15 si les
        refus dominent. Un outil jamais évalué reste neutre (0.0).
        """
        with self._lock:
            conn = self._connect()
            counts = self._counts(conn)
            conn.close()
        acc, rej = counts.get(tool or "", (0, 0))
        if acc == 0 and rej == 0:
            return 0.0
        if rej > acc:
            return -0.15
        return min(0.3, 0.1 * acc)

    def count(self) -> int:
        with self._lock:
            conn = self._connect()
            n = conn.execute("SELECT COUNT(*) FROM copilot_feedback").fetchone()[0]
            conn.close()
        return int(n)


# --- Singleton paresseux ------------------------------------------------------

_feedback_store: FeedbackStore | None = None
_feedback_lock = threading.Lock()


def get_feedback_store() -> FeedbackStore:
    global _feedback_store
    with _feedback_lock:
        if _feedback_store is None:
            _feedback_store = FeedbackStore()
        return _feedback_store


def reset_feedback_store(path: str | None = None) -> FeedbackStore:
    """Recharge le store (utilisé par les tests pour isoler la base)."""
    global _feedback_store
    with _feedback_lock:
        _feedback_store = FeedbackStore(path) if path else FeedbackStore()
        return _feedback_store
