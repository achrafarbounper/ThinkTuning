"""Migration en place des contenus de sessions corrompus (mojibake UTF-8/Latin-1).

AVERTISSEMENT : une fois la cause racine corrigée dans ``ia/agent/llm_client.py``
et la lecture réparée dans ``core/session_store.py``, ce script est OPTIONNEL :
les anciens messages sont déjà affichés corrigés à la lecture. Il ne nettoie
que si l'on veut réécrire physiquement la base.

Ce que fait le script :
  1. crée une sauvegarde ``<db>.bak`` (s'il n'existe pas déjà) ;
  2. (dry-run par défaut) liste les messages/titres/résumés réparables et le
     nombre de cellules qui changeraient ;
  3. avec ``--apply``, réécrit ces cellules dans la base.

Usage :
  venv\\Scripts\\python.exe scripts\\repair_session_encodings.py                 # dry-run
  venv\\Scripts\\python.exe scripts\\repair_session_encodings.py --apply         # écrire
  venv\\Scripts\\python.exe scripts\\repair_session_encodings.py --db <chemin>   # autre base
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

# Rend le paquet racine importable (le script vit dans scripts/).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ia.agent.encoding import repair_utf8_mojibake  # noqa: E402


def _repaired_cells(conn: sqlite3.Connection):
    """Itère (table, colonne, id, valeur_brute, valeur_réparée) quand une
    cellule change réellement après réparation."""
    _TABLES = (
        ("agent_session_messages", "id", "content"),
        ("agent_sessions", "id", "title"),
        ("agent_memory", "key", "summary"),
    )
    for table, pk_col, col in _TABLES:
        try:
            rows = conn.execute(f"SELECT {pk_col}, {col} FROM {table}").fetchall()
        except sqlite3.OperationalError:
            continue  # table absente (ex. ancienne base sans mémoire)
        for pk, raw in rows:
            if raw is None:
                continue
            fixed = repair_utf8_mojibake(raw)
            if fixed != raw:
                yield table, col, pk, raw, fixed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        default=os.getenv("AGENT_SESSION_PATH", os.path.join("experiments", "agent_sessions.db")),
        help="Chemin de la base sessions (défaut : AGENT_SESSION_PATH ou experiments/agent_sessions.db).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Écrit réellement les corrections (sinon : affichage seul, dry-run).",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"[repair] Base introuvable : {db_path}")
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        cells = list(_repaired_cells(conn))
    finally:
        conn.close()

    by_table: dict[str, int] = {}
    for table, col, pk, raw, fixed in cells:
        by_table[f"{table}.{col}"] = by_table.get(f"{table}.{col}", 0) + 1
        print(f"  [{table}.{col}] id={pk!r}")
        print(f"    avant : {raw[:120]!r}")
        print(f"    après : {fixed[:120]!r}")

    total = len(cells)
    print(f"\n[repair] {total} cellule(s) réparable(s) : {by_table or 'aucune'}")

    if not args.apply:
        print("[repair] dry-run : aucun changement écrit. Relancez avec --apply pour appliquer.")
        return 0 if total else 0

    if total == 0:
        print("[repair] Rien à faire.")
        return 0

    # Sauvegarde unique avant toute écriture.
    backup = db_path.with_suffix(db_path.suffix + ".bak")
    if not backup.exists():
        shutil.copy2(str(db_path), str(backup))
        print(f"[repair] Sauvegarde créée : {backup}")
    else:
        print(f"[repair] Sauvegarde déjà présente (non écrasée) : {backup}")

    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            for table, col, pk, raw, fixed in cells:
                conn.execute(
                    f"UPDATE {table} SET {col} = ? WHERE id = ?", (fixed, pk)
                )
    finally:
        conn.close()
    print(f"[repair] {total} cellule(s) réécrite(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())