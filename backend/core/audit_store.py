# project/core/audit_store.py

"""Journal d'audit & conformité des actions sensibles de l'agent (SQLite).

Répond aux exigences de conformité (Phase A — Security & Compliance) : toute
action à fort enjeu (lancement de run, exécution d'outil, décision de
validation humaine, modification de config) est tracée dans une table dédiée
avec, pour chaque entrée, un identifiant stable, l'acteur, l'action, le sujet,
le détail JSON, l'IP d'origine, et le run/requête lié — le tout horodaté en
ISO UTC (millisecondes).

Mêmes conventions que ``core/approval_store.py`` / ``core/run_store.py`` :

    - base SQLite dédiée (experiments/agent_audit.db, surchargeable via
      AGENT_AUDIT_PATH pour isoler les tests) ;
    - store thread-safe (l'API FastAPI appelle depuis plusieurs threads) ;
    - singleton paresseux ``get_audit_store()`` + ``reset_audit_store()``.

Sécurité des données : ``redact()`` est appliqué à l'écriture sur le détail
pour ne JAMAIS persister de secret en clair — clés API, jetons et champs nommés
d'après ``SENSITIVE_KEYS`` sont remplacés par ``[REDACTED]``, et les autres
valeurs trop longues sont tronquées.
"""

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

AGENT_AUDIT_PATH = os.getenv(
    "AGENT_AUDIT_PATH", os.path.join("experiments", "agent_audit.db")
)

# Actions d'audit normalisées (le code appelant peut en définir d'autres).
ACT_RUN = "agent_run"            # lancement / issue d'un run
ACT_TOOL = "tool_execution"      # appel d'un outil
ACT_APPROVAL = "approval"        # décision approve / reject
ACT_CONFIG = "config_change"     # modification de la configuration LLM
ACT_CONNECT = "connectivity"     # sonde de connectivité provider

# Clés sensibles à anonymiser dans le détail (comparaison insensible à la casse).
SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "token",
    "authorization",
    "secret",
    "password",
    "passwd",
    "pwd",
    "key",
}

# Taille max de chaque champ du détail JSON après anonymisation.
_MAX_STRING_CHARS = 2000
_MAX_DETAIL_ITEMS = 200

_COLUMNS = [
    "id",
    "ts",
    "actor",
    "action",
    "subject",
    "detail_json",
    "ip",
    "request_id",
    "run_id",
]


def _utcnow_iso() -> str:
    """Horodatage ISO 8601 UTC (millisecondes) — stable, triable, horodaté."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _truncate(text: str) -> str:
    suffix = "…[tronqué]"
    if len(text) <= _MAX_STRING_CHARS:
        return text
    # La longueur TOTALE (texte + suffixe) reste dans la limite.
    keep = max(0, _MAX_STRING_CHARS - len(suffix))
    return text[:keep] + suffix


def redact(value):
    """Anonymise récursivement une valeur (dict/list/scalaire) pour l'audit.

    - Les clés nommées comme dans ``SENSITIVE_KEYS`` (insensible à la casse)
      sont remplacées par ``[REDACTED]`` (valeur écrasée) ;
    - les chaînes trop longues sont tronquées ;
    - les listes/objets volumineux sont bornés pour garder une trace lisible.
    """
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.strip().lower() in SENSITIVE_KEYS:
                out[k] = "[REDACTED]"
            else:
                out[k] = redact(v)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value][:_MAX_DETAIL_ITEMS]
    if value is None:
        return None
    if isinstance(value, str):
        return _truncate(value)
    # Scalaires typés (int/float/bool) : préserver le type.
    return value


def _dumps(detail) -> str:
    try:
        return json.dumps(redact(detail or {}), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps({"__detail_error__": str(detail)[:200]}, ensure_ascii=False)
class AuditStore:
    """Journal d'audit au-dessus d'une table SQLite (thread-safe)."""

    def __init__(self, path: str = AGENT_AUDIT_PATH):
        self.path = path
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._ensure_db()

    # --- Infra ---

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30.0)

    def _ensure_db(self):
        conn = self._connect()
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_audit (
                id          TEXT PRIMARY KEY,
                ts          TEXT NOT NULL,
                actor       TEXT NOT NULL,
                action      TEXT NOT NULL,
                subject     TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}',
                ip          TEXT,
                request_id  TEXT NOT NULL DEFAULT '',
                run_id      TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON agent_audit(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_action ON agent_audit(action)")
        conn.commit()
        conn.close()

    @staticmethod
    def _row_to_dict(row) -> dict | None:
        if row is None:
            return None
        data = dict(zip(_COLUMNS, row))
        try:
            data["detail"] = json.loads(data.pop("detail_json") or "{}")
        except ValueError:
            data["detail"] = {}
        return data

    # --- Écriture ---

    def log(
        self,
        action: str,
        subject: str = "",
        detail: dict | None = None,
        actor: str = "system",
        ip: str | None = None,
        request_id: str | None = None,
        run_id: str | None = None,
    ) -> dict:
        """Ajoute une entrée d'audit (détail anonymise/trongure a l'ecriture)."""
        record_id = str(uuid.uuid4())
        ts = _utcnow_iso()
        with self._lock:
            conn = self._connect()
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO agent_audit (id, ts, actor, action, subject,"
                        " detail_json, ip, request_id, run_id)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            record_id,
                            ts,
                            actor or "system",
                            action,
                            subject or "",
                            _dumps(detail),
                            ip,
                            request_id or "",
                            run_id or "",
                        ),
                    )
            finally:
                conn.close()
        row = self.get(record_id)
        assert row is not None
        return row

    # --- Lecture ---

    def get(self, audit_id: str) -> dict | None:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM agent_audit WHERE id = ?", (str(audit_id),)
                ).fetchone()
            finally:
                conn.close()
        return self._row_to_dict(row)

    def query(
        self,
        action: str | None = None,
        subject: str | None = None,
        actor: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> dict:
        """Interroge le journal (filtres AND). Retourne items/total/limit/offset."""
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        conditions, params = [], []
        if action:
            conditions.append("action = ?"); params.append(action)
        if subject:
            conditions.append("subject = ?"); params.append(subject)
        if actor:
            conditions.append("actor = ?"); params.append(actor)
        if run_id:
            conditions.append("run_id = ?"); params.append(run_id)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        with self._lock:
            conn = self._connect()
            try:
                total = conn.execute(
                    f"SELECT COUNT(*) FROM agent_audit{where}", params,
                ).fetchone()[0]
                rows = conn.execute(
                    f"SELECT * FROM agent_audit{where}"
                    " ORDER BY ts DESC, id LIMIT ? OFFSET ?",
                    [*params, limit, offset],
                ).fetchall()
            finally:
                conn.close()
        return {
            "items": [d for d in (self._row_to_dict(r) for r in rows) if d is not None],
            "total": total,
            "limit": limit,
            "offset": offset,
        }


# --- Store partagé (lazy, surchargeable en tests) ---

_store: AuditStore | None = None
_store_lock = threading.Lock()


def _current_path() -> str:
    """Chemin effectif de la base, relu à chaque création de store.

    Permet à l'env var AGENT_AUDIT_PATH de surcharger le chemin par défaut
    même après l'import du module (isolation des tests via monkeypatch).
    """
    return os.getenv("AGENT_AUDIT_PATH") or AGENT_AUDIT_PATH


def get_audit_store() -> AuditStore:
    """Store partagé de l'application (instance unique paresseuse)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = AuditStore(_current_path())
        return _store


def reset_audit_store(path: str | None = None) -> AuditStore:
    """Remplace le store partagé par une base neuve (isolation des tests)."""
    global _store
    with _store_lock:
        _store = AuditStore(path or _current_path())
        return _store
