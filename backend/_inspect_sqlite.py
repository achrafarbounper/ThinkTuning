"""Inspection temporaire : schémas + contenu des bases SQLite legacy (migration Mongo)."""
import sqlite3
from pathlib import Path

for name in ("agent_sessions.db", "agent_settings.db"):
    path = Path("backend/experiments") / name
    print(f"=== {path} (exists={path.exists()}) ===")
    if not path.exists():
        continue
    con = sqlite3.connect(path)
    for (table, sql) in con.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='table'"
    ):
        print(sql)
        count = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"rows: {count}")
        for row in con.execute(f"SELECT * FROM {table} LIMIT 5"):
            print("  ", row)
    con.close()
