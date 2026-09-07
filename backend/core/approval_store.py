# project/core/approval_store.py

"""File de validation humaine des actions de l'agent IA (SQLite).

Le moteur de décision (``ia/agent/approvals.py``) classe chaque appel d'outil
en auto_approve / approve / reject. Ce module persiste les actions qui
nécessitent une décision humaine (``approve``) et celles bloquées (``reject``)
pour garantir la TRAÇABILITÉ : chaque entrée porte un identifiant stable, le
JSON des arguments, la catégorie, la raison et un horodatage ISO (UTC).

Mêmes conventions que ``core/agent_settings.py`` :
    - base SQLite dédiée (experiments/agent_approvals.db, surchargeable via
      AGENT_APPROVAL_PATH pour isoler les tests) ;
    - une table unique ``agent_approvals`` avec un store thread-safe.

Flux « approve » (validation humaine) :
    1. l'agent classe une action ``approve`` ;
    2. le store crée une ligne ``status='pending'`` (jamais exécutée) ;
    3. l'utilisateur valide → ``approve(id)`` (status ``approved``) ou
       refuse → ``reject(id)`` (status ``rejected``) ;
    4. l'agent ``resume`` la tâche avec l'action approuvée, qu'il exécute
       alors une seule fois, puis conclut.
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

AGENT_APPROVAL_PATH = os.getenv(
    "AGENT_APPROVAL_PATH", os.path.join("experiments", "agent_approvals.db")
)

# Statuts possibles d'une demande. Seule une ligne ``pending`` peut être
# décidée ; ``approved`` autorise la reprise (exécution), ``rejected`` non.
PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"

STATUSES = (PENDING, APPROVED, REJECTED)

_COLUMNS = [
    "id",
    "tool",
    "args_json",
    "category",
    "decision",
    "reason",
    "status",
    "prompt",
    "request_id",
    "args_hash",
    "created_at",
    "decided_by",
    "decided_at",
]


def _utcnow_iso() -> str:
    """Horodatage ISO 8601 UTC (millisecondes) — stable, triable, horodaté."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class ApprovalStore:
    """File d'approbations au-dessus d'une table SQLite (thread-safe)."""

    def __init__(self, path: str = AGENT_APPROVAL_PATH):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._ensure_db()

    # --- Infra ----------------------------------------------------------------------

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30.0)

    def _ensure_db(self):
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_approvals (
                id          TEXT PRIMARY KEY,
                tool        TEXT NOT NULL,
                args_json   TEXT NOT NULL,
                category    TEXT NOT NULL,
                decision    TEXT NOT NULL,
                reason      TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                prompt      TEXT NOT NULL DEFAULT '',
                request_id  TEXT NOT NULL,
                args_hash   TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL,
                decided_by  TEXT,
                decided_at  TEXT
            )
            """
        )
        conn.commit()
        conn.close()

    def _normalize_args(self, args) -> dict:
        """Les arguments peuvent être un dict (cas normal) ou toute autre valeur."""
        if isinstance(args, dict):
            return args
        return {"value": args}

    def _row_to_dict(self, row) -> dict | None:
        if row is None:
            return None
        data = dict(zip(_COLUMNS, row))
        data["args"] = json.loads(data.pop("args_json"))
        return data

    # --- Création --------------------------------------------------------------------

    def create(
        self,
        tool: str,
        args,
        category: str,
        decision: str,
        reason: str,
        prompt: str = "",
        args_hash: str = "",
        status: str = PENDING,
    ) -> str:
        """Crée une demande et renvoie son identifiant.

        ``status`` : ``pending`` pour une action en attente de validation
        (approve), ``rejected`` pour une action bloquée (reject) — la même
        table sert de trace dans les deux cas.
        """
        effective_status = status if status in STATUSES else PENDING
        request_id = uuid.uuid4().hex
        now = _utcnow_iso()
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        """
                        INSERT INTO agent_approvals(
                            id, tool, args_json, category, decision, reason,
                            status, prompt, request_id, args_hash, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            request_id,
                            str(tool),
                            json.dumps(self._normalize_args(args), ensure_ascii=False),
                            str(category),
                            str(decision),
                            str(reason),
                            effective_status,
                            str(prompt),
                            request_id,
                            str(args_hash),
                            now,
                        ),
                    )
            finally:
                conn.close()
        return request_id

    # --- Lecture ---------------------------------------------------------------------

    def get(self, request_id: str) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM agent_approvals WHERE id = ?", (str(request_id),)
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_dict(row)

    def list(self, status: str | None = None) -> list[dict]:
        """Liste des demandes. ``status`` filtre ; None = toutes (récentes d'abord)."""
        with self._lock:
            conn = self._connect()
            try:
                if status:
                    rows = conn.execute(
                        "SELECT * FROM agent_approvals WHERE status = ? "
                        "ORDER BY created_at DESC",
                        (status,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM agent_approvals ORDER BY created_at DESC"
                    ).fetchall()
            finally:
                conn.close()
        return [self._row_to_dict(row) for row in rows]

    # --- Décisions --------------------------------------------------------------------

    def approve(self, request_id: str, decided_by: str | None = None) -> dict | None:
        """Valide une demande → ``approved``. Retourne la ligne (None si absente)."""
        return self._decide(request_id, APPROVED, decided_by)

    def reject(self, request_id: str, decided_by: str | None = None) -> dict | None:
        """Refuse une demande → ``rejected``. Retourne la ligne (None si absente)."""
        return self._decide(request_id, REJECTED, decided_by)

    def _decide(self, request_id, status, decided_by) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                existing = conn.execute(
                    "SELECT status FROM agent_approvals WHERE id = ?", (str(request_id),)
                ).fetchone()
                if existing is None:
                    return None
                now = _utcnow_iso()
                with conn:
                    conn.execute(
                        """
                        UPDATE agent_approvals
                        SET status = ?, decided_by = ?, decided_at = ?
                        WHERE id = ?
                        """,
                        (status, decided_by, now, str(request_id)),
                    )
                row = conn.execute(
                    "SELECT * FROM agent_approvals WHERE id = ?", (str(request_id),)
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_dict(row)


# --- Store partagé (lazy, surchargeable en tests) ------------------------------------

_store: ApprovalStore | None = None
_store_lock = threading.Lock()


def get_approval_store() -> ApprovalStore:
    """Store partagé de l'application (instance unique paresseuse)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = ApprovalStore(AGENT_APPROVAL_PATH)
        return _store


def reset_approval_store(path: str | None = None) -> ApprovalStore:
    """Remplace le store partagé par une base neuve (isolation des tests)."""
    global _store
    with _store_lock:
        _store = ApprovalStore(path or AGENT_APPROVAL_PATH)
        return _store