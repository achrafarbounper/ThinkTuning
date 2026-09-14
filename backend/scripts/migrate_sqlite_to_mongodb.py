#!/usr/bin/env python
"""Migration ONE-SHOT des bases SQLite legacy vers MongoDB (ADR-0004, SCRUM-137).

Copie le contenu des bases SQLite historiques du backend vers les collections
MongoDB du runtime — les MÊMES collections que les ``Mongo*Store`` écrivent :

    experiments/agent_sessions.db
      ├─ agent_sessions                                → agent_sessions
      ├─ agent_session_messages                        → agent_session_messages
      └─ agent_memory                                  → agent_memory
    experiments/agent_settings.db
      └─ agent_settings                                → agent_settings

Convention de copie (parité avec les stores runtime) :

    - sessions      : ``_id = id`` (hex 12), champs title/model/created_at/updated_at ;
    - messages      : ``_id = "sqlite-<rowid>"`` (idempotence des re-runs),
      ``id = str(rowid)``, ``tool_calls`` décodé depuis ``tool_calls_json``,
      ``_order = rowid`` (préserve l'ordre d'insertion ; les messages migrés
      trient AVANT tout message runtime, dont ``_order = time.time_ns()``) ;
    - mémoire       : ``_id = key`` ;
    - paramètres    : ``_id = key``, ``value`` copié TEL QUEL (chaîne JSON) —
      ``MongoAgentSettingsStore.get_all`` normalise nativement à la première
      lecture (``_decode_settings_value``, cf. persistence/common.py).

Usage :

    python scripts/migrate_sqlite_to_mongodb.py --dry-run    # simulation
    python scripts/migrate_sqlite_to_mongodb.py              # copie (skip docs existants)
    python scripts/migrate_sqlite_to_mongodb.py --upsert     # écrase les docs existants
    python scripts/migrate_sqlite_to_mongodb.py --archive    # renomme les .db après succès

La cible est résolue comme au runtime : ``MONGODB_URI`` / ``MONGODB_DATABASE``
(depuis ``backend/.env``). ``MONGODB_MOCK=1`` est refusé sauf ``--allow-mock``
(une migration vers mongomock ne persiste rien).

Sortie : 0 si tout est copié/vérifié, 1 en cas d'erreur (fichier absent, Mongo
injoignable…). Idempotent : les docs déjà présents sont comptés « skipped »
(sauf ``--upsert``).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Racine backend/ (le script vit dans backend/scripts/).
BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_SESSIONS_DB = BACKEND_DIR / "experiments" / "agent_sessions.db"
DEFAULT_SETTINGS_DB = BACKEND_DIR / "experiments" / "agent_settings.db"


def _connect_sqlite(path: Path) -> sqlite3.Connection:
    """Ouvre la base SQLite legacy en lecture seule (garde-fou anti-écriture)."""
    if not path.exists():
        raise FileNotFoundError(f"Base SQLite introuvable : {path}")
    uri = f"file:{path.as_posix()}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.row_factory = sqlite3.Row
    return con


def _upsert_doc(coll: Any, doc: dict[str, Any], doc_id: str, upsert: bool, dry: bool) -> str:
    """Écrit (ou simule) un document ; renvoie 'copied' ou 'skipped'."""
    if dry:
        exists = coll.count_documents({"_id": doc_id}, limit=1) > 0
        return "skipped" if exists and not upsert else "copied"
    if not upsert and coll.count_documents({"_id": doc_id}, limit=1) > 0:
        return "skipped"
    coll.update_one({"_id": doc_id}, {"$set": doc}, upsert=True)
    return "copied"


def _migrate_table(
    con: sqlite3.Connection,
    coll: Any,
    select_sql: str,
    row_to_doc: Callable[[sqlite3.Row], tuple[str, dict[str, Any]]],
    label: str,
    upsert: bool,
    dry: bool,
) -> tuple[int, int]:
    """Copie une table vers une collection ; renvoie (copiés, ignorés)."""
    copied = skipped = 0
    for row in con.execute(select_sql):
        doc_id, doc = row_to_doc(row)
        if _upsert_doc(coll, doc, doc_id, upsert, dry) == "copied":
            copied += 1
        else:
            skipped += 1
    print(f"  {label:<42} copiés={copied:<5} ignorés(déjà en Mongo)={skipped}")
    return copied, skipped


def _row_session(row: sqlite3.Row) -> tuple[str, dict[str, Any]]:
    return str(row["id"]), {
        "_id": str(row["id"]),
        "id": str(row["id"]),
        "title": row["title"] or "",
        "model": row["model"] or "",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _row_message(row: sqlite3.Row) -> tuple[str, dict[str, Any]]:
    try:
        tool_calls = json.loads(row["tool_calls_json"] or "[]")
    except (TypeError, ValueError):
        tool_calls = []
    if not isinstance(tool_calls, list):
        tool_calls = []
    return f"sqlite-{row['id']}", {
        "_id": f"sqlite-{row['id']}",
        "id": str(row["id"]),
        "session_id": str(row["session_id"]),
        "role": row["role"],
        "content": row["content"] or "",
        "thinking": row["thinking"] or "",
        "tool_calls": tool_calls,
        "created_at": row["created_at"],
        "_order": int(row["id"]),  # cf. docstring : ordre d'insertion préservé
    }


def _row_memory(row: sqlite3.Row) -> tuple[str, dict[str, Any]]:
    return str(row["key"]), {
        "_id": str(row["key"]),
        "key": str(row["key"]),
        "summary": row["summary"] or "",
        "updated_at": row["updated_at"],
    }


def _row_setting(row: sqlite3.Row) -> tuple[str, dict[str, Any]]:
    return str(row["key"]), {
        "_id": str(row["key"]),
        "key": str(row["key"]),
        "value": row["value"],  # copié TEL QUEL (chaîne JSON) — cf. docstring
        "updated_at": float(row["updated_at"]),
    }


def _archive(path: Path) -> Path | None:
    """Renomme la base migrée (horodatée) ; gère aussi -wal/-shm éventuels."""
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.migrated-{stamp}")
    path.rename(target)
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            side.rename(side.with_name(side.name + f".migrated-{stamp}"))
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Migration SQLite legacy → MongoDB")
    parser.add_argument("--sessions-db", type=Path, default=DEFAULT_SESSIONS_DB)
    parser.add_argument("--settings-db", type=Path, default=DEFAULT_SETTINGS_DB)
    parser.add_argument("--dry-run", action="store_true", help="Simule sans écrire")
    parser.add_argument("--upsert", action="store_true", help="Écrase les docs Mongo existants")
    parser.add_argument(
        "--archive", action="store_true", help="Renomme les .db en *.migrated-<ts> après succès"
    )
    parser.add_argument(
        "--allow-mock",
        action="store_true",
        help="Autorise MONGODB_MOCK=1 (mongomock ne persiste rien — répétition locale)",
    )
    args = parser.parse_args()

    from app.infrastructure.persistence.common import get_mongo_provider

    provider = get_mongo_provider()
    if str(type(provider.client).__module__).startswith("mongomock") and not args.allow_mock:
        print(
            "REFUS : MONGODB_MOCK=1 détecté (mongomock ne persiste rien).\n"
            "Retirer MONGODB_MOCK ou relancer avec --allow-mock pour une répétition."
        )
        return 1

    db = provider.db
    total = {"copied": 0, "skipped": 0}
    mode = "DRY-RUN" if args.dry_run else "MIGRATION"
    print(f"{mode} → base Mongo '{db.name}'")

    # --- 1) Sessions + messages + mémoire -----------------------------------
    sessions_con = _connect_sqlite(args.sessions_db)
    try:
        stats = _migrate_table(
            sessions_con, db["agent_sessions"],
            "SELECT * FROM agent_sessions", _row_session,
            "agent_sessions → agent_sessions", args.upsert, args.dry_run,
        )
        total["copied"] += stats[0]
        total["skipped"] += stats[1]
        stats = _migrate_table(
            sessions_con, db["agent_session_messages"],
            "SELECT * FROM agent_session_messages", _row_message,
            "agent_session_messages → agent_session_messages", args.upsert, args.dry_run,
        )
        total["copied"] += stats[0]
        total["skipped"] += stats[1]
        stats = _migrate_table(
            sessions_con, db["agent_memory"],
            "SELECT * FROM agent_memory", _row_memory,
            "agent_memory → agent_memory", args.upsert, args.dry_run,
        )
        total["copied"] += stats[0]
        total["skipped"] += stats[1]
    finally:
        sessions_con.close()

    # --- 2) Paramètres agent (module IHM) ------------------------------------
    settings_con = _connect_sqlite(args.settings_db)
    try:
        stats = _migrate_table(
            settings_con, db["agent_settings"],
            "SELECT * FROM agent_settings", _row_setting,
            "agent_settings → agent_settings", args.upsert, args.dry_run,
        )
        total["copied"] += stats[0]
        total["skipped"] += stats[1]
    finally:
        settings_con.close()

    print(f"\nTotal : {total['copied']} document(s) copié(s), {total['skipped']} ignoré(s).")

    if args.archive and not args.dry_run:
        for path in (args.sessions_db, args.settings_db):
            target = _archive(path)
            print(f"Archivé : {path} → {target}" if target else f"(déjà absent : {path})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
