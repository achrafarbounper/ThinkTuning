# project/core/session_store.py

"""Persistance des conversations de l'assistant (« sessions ») — SQLite.

Permet au dashboard de retrouver ses conversations après un rechargement :
chaque session porte un titre (auto-dérivé du premier message utilisateur),
un modèle LLM et une liste ordonnée de messages (rôle, contenu, appels
d'outils éventuels en JSON pour le mode Agent).

Mêmes conventions que ``core/approval_store.py`` / ``core/run_store.py`` :
base SQLite dédiée (experiments/agent_sessions.db, surchargeable via
AGENT_SESSION_PATH) et store thread-safe.
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

# Réparation des doubles-encodages UTF-8 → Latin-1 → UTF-8 à la lecture des
# contenus persistés (voir ia/agent/encoding.py). Fonction impure, sans autres
# imports que la stdlib, donc aucun risque d'import circulaire avec ``ia.*``.
from ia.agent.encoding import repair_utf8_mojibake

AGENT_SESSION_PATH = os.getenv(
    "AGENT_SESSION_PATH", os.path.join("experiments", "agent_sessions.db")
)

_ROLES = ("user", "assistant")

_SELECT_SESSION = "id, title, model, created_at, updated_at"
_SELECT_MESSAGE = "id, session_id, role, content, tool_calls_json, created_at"


def _utcnow_iso() -> str:
    """Horodatage ISO 8601 UTC (millisecondes) — stable, triable."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class SessionStore:
    """Conversations de l'assistant au-dessus d'une base SQLite (thread-safe)."""

    def __init__(self, path: str = AGENT_SESSION_PATH):
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
            CREATE TABLE IF NOT EXISTS agent_sessions (
                id         TEXT PRIMARY KEY,
                title      TEXT NOT NULL DEFAULT '',
                model      TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_session_messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL DEFAULT '',
                tool_calls_json TEXT NOT NULL DEFAULT '[]',
                created_at      TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES agent_sessions(id)
                    ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_messages ON "
            "agent_session_messages(session_id)"
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _session_row(row):
        if row is None:
            return None
        data = dict(zip(("id", "title", "model", "created_at", "updated_at"), row))
        # Réparation des doublons d'encodage éventuels persistés avant la
        # correction de la cause racine (voir ia/agent/encoding.py).
        data["title"] = repair_utf8_mojibake(data.get("title") or "")
        return data

    @staticmethod
    def _message_row(row):
        if row is None:
            return None
        data = dict(zip(("id", "session_id", "role", "content", "tool_calls_json",
                         "created_at"), row))
        data["content"] = repair_utf8_mojibake(data.get("content") or "")
        try:
            data["tool_calls"] = json.loads(data.pop("tool_calls_json") or "[]")
        except ValueError:
            data["tool_calls"] = []
        return data

    # --- Sessions ----------------------------------------------------------------

    def create_session(self, title: str = "", model: str = "") -> dict:
        """Crée une session vide ; titre dérivé de la date s'il est absent."""
        session_id = uuid.uuid4().hex[:12]
        now = _utcnow_iso()
        title = (title or "").strip() or f"Conversation du {now[:10]}"
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO agent_sessions (id, title, model, created_at,"
                        " updated_at) VALUES (?, ?, ?, ?, ?)",
                        (session_id, title, model or "", now, now),
                    )
            finally:
                conn.close()
        return self.get_session(session_id) or {"id": session_id, "title": title}

    def get_session(self, session_id: str) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    f"SELECT {_SELECT_SESSION} FROM agent_sessions WHERE id = ?",
                    (str(session_id),),
                ).fetchone()
            finally:
                conn.close()
        return self._session_row(row)

    def list_sessions(self, limit: int = 100) -> list[dict]:
        """Sessions les plus récemment actives d'abord."""
        limit = max(1, min(int(limit), 200))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT {_SELECT_SESSION} FROM agent_sessions "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            finally:
                conn.close()
        return [s for s in (self._session_row(r) for r in rows) if s is not None]

    def rename_session(self, session_id: str, title: str) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    cursor = conn.execute(
                        "UPDATE agent_sessions SET title = ? WHERE id = ?",
                        ((title or "").strip(), str(session_id)),
                    )
                if cursor.rowcount == 0:
                    return None
                row = conn.execute(
                    f"SELECT {_SELECT_SESSION} FROM agent_sessions WHERE id = ?",
                    (str(session_id),),
                ).fetchone()
            finally:
                conn.close()
        return self._session_row(row)

    def delete_session(self, session_id: str) -> bool:
        """Supprime la session ET ses messages. True si la session existait."""
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    deleted = conn.execute(
                        "DELETE FROM agent_sessions WHERE id = ?", (str(session_id),)
                    ).rowcount
                    # Pas de garantie PRAGMA foreign_keys sous tous les drivers :
                    # suppression explicite des messages.
                    conn.execute(
                        "DELETE FROM agent_session_messages WHERE session_id = ?",
                        (str(session_id),),
                    )
            finally:
                conn.close()
        return deleted > 0

    # --- Messages ---------------------------------------------------------------

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict] | None = None,
    ) -> dict | None:
        """Ajoute un message ; met à jour l'activité et titre automatiquement.

        Le premier message ``user`` nomme la session (60 premiers caractères).
        Retourne le message créé, ou None si la session n'existe pas ; lève
        ``ValueError`` pour un rôle invalide.
        """
        role = (role or "").strip().lower()
        if role not in _ROLES:
            raise ValueError(f"Rôle inconnu : '{role}'. Attendus : {', '.join(_ROLES)}")
        payload = json.dumps(tool_calls or [], ensure_ascii=False)
        now = _utcnow_iso()
        with self._lock:
            conn = self._connect()
            try:
                exists = conn.execute(
                    "SELECT 1 FROM agent_sessions WHERE id = ?", (str(session_id),)
                ).fetchone()
                if exists is None:
                    return None
                with conn:
                    conn.execute(
                        "INSERT INTO agent_session_messages (session_id, role,"
                        " content, tool_calls_json, created_at) VALUES (?, ?, ?, ?, ?)",
                        (str(session_id), role, content or "", payload, now),
                    )
                    conn.execute(
                        "UPDATE agent_sessions SET updated_at = ? WHERE id = ?",
                        (now, str(session_id)),
                    )
                    if role == "user":
                        # Titre auto une seule fois : uniquement quand encore générique.
                        first_user = conn.execute(
                            "SELECT COUNT(*) FROM agent_session_messages"
                            " WHERE session_id = ? AND role = 'user'",
                            (str(session_id),),
                        ).fetchone()[0]
                        existing_title = conn.execute(
                            "SELECT title FROM agent_sessions WHERE id = ?",
                            (str(session_id),),
                        ).fetchone()[0]
                        if first_user == 1 and existing_title.startswith("Conversation du "):
                            title = (content or "").strip()[:60] or existing_title
                            conn.execute(
                                "UPDATE agent_sessions SET title = ? WHERE id = ?",
                                (title, str(session_id)),
                            )
                row = conn.execute(
                    f"SELECT {_SELECT_MESSAGE} FROM agent_session_messages"
                    " ORDER BY id DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
        return self._message_row(row)

    def get_messages(self, session_id: str, limit: int = 200) -> list[dict]:
        """Messages d'une session dans l'ordre chronologique."""
        limit = max(1, min(int(limit), 500))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    f"SELECT {_SELECT_MESSAGE} FROM agent_session_messages"
                    " WHERE session_id = ? ORDER BY id ASC LIMIT ?",
                    (str(session_id), limit),
                ).fetchall()
            finally:
                conn.close()
        return [m for m in (self._message_row(r) for r in rows) if m is not None]

    # --- Mémoire inter-sessions (Phase C, flag AGENT_CONTEXT) -------------------

    def _ensure_memory_table(self, conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_memory (
                key        TEXT PRIMARY KEY,
                summary    TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )

    def save_memory(self, key: str, summary: str) -> None:
        """Enregistre (upsert) un résumé de mémoire pour la clé donnée.

        ``key`` est typiquement l'identifiant d'espace mémoire (ex. l'id de
        session pour la mémoire glissante d'une conversation, ou ``"global"``
        pour une mémoire partagée entre sessions). Best-effort chez l'appelant.
        """
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_memory_table(conn)
                conn.execute(
                    """
                    INSERT INTO agent_memory(key, summary, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        summary = excluded.summary,
                        updated_at = excluded.updated_at
                    """,
                    (str(key), str(summary or ""), _utcnow_iso()),
                )
                conn.commit()
            finally:
                conn.close()

    def get_memory(self, key: str) -> str:
        """Résumé de mémoire associé à la clé (chaîne vide si absent)."""
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_memory_table(conn)
                row = conn.execute(
                    "SELECT summary FROM agent_memory WHERE key = ?",
                    (str(key),),
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return ""
        return repair_utf8_mojibake(str(row[0]))

    def delete_memory(self, key: str) -> None:
        """Supprime une entrée de mémoire (nettoyage / tests)."""
        with self._lock:
            conn = self._connect()
            try:
                self._ensure_memory_table(conn)
                conn.execute(
                    "DELETE FROM agent_memory WHERE key = ?", (str(key),)
                )
                conn.commit()
            finally:
                conn.close()


# --- Store partagé (lazy, surchargeable en tests) ------------------------------------

_store: SessionStore | None = None
_store_lock = threading.Lock()


def get_session_store() -> SessionStore:
    """Store partagé de l'application (instance unique paresseuse)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = SessionStore(AGENT_SESSION_PATH)
        return _store


def reset_session_store(path: str | None = None) -> SessionStore:
    """Remplace le store partagé par une base neuve (isolation des tests)."""
    global _store
    with _store_lock:
        _store = SessionStore(path or AGENT_SESSION_PATH)
        return _store
