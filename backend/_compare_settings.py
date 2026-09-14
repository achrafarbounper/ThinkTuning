"""Diagnostic temporaire : comparaison updated_at SQLite vs Mongo (agent_settings)."""
import sqlite3

from app.infrastructure.persistence.common import get_mongo_provider

con = sqlite3.connect("experiments/agent_settings.db")
con.row_factory = sqlite3.Row
sqlite_rows = {r["key"]: r["updated_at"] for r in con.execute("SELECT key, updated_at FROM agent_settings")}
con.close()

provider = get_mongo_provider()
mongo_rows = {d["key"]: d.get("updated_at") for d in provider.db["agent_settings"].find({})}

newer_in_mongo = older_in_mongo = equal = missing = 0
for key, ts in sqlite_rows.items():
    m = mongo_rows.get(key)
    if m is None:
        missing += 1
    elif m > ts:
        newer_in_mongo += 1
    elif m < ts:
        older_in_mongo += 1
    else:
        equal += 1

print(f"SQLite={len(sqlite_rows)} docs, Mongo={len(mongo_rows)} docs")
print(f"Mongo plus récent: {newer_in_mongo} | SQLite plus récent: {older_in_mongo} | égaux: {equal} | absents de Mongo: {missing}")
for key, ts in sorted(sqlite_rows.items()):
    m = mongo_rows.get(key)
    if m is not None and m < ts:
        print(f"  SQLITE PLUS RÉCENT: {key} sqlite={ts} mongo={m}")
