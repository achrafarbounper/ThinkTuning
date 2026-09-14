from __future__ import annotations

import argparse
import json

from app.infrastructure.persistence.mcp_mongo_run_store import MongoMCPDurableRunStore
from app.infrastructure.persistence.mcp_sqlite_migration import migrate_sqlite_to_mongo


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate MCP durable runs from SQLite to MongoDB."
    )
    parser.add_argument("--sqlite-path", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    result = migrate_sqlite_to_mongo(
        args.sqlite_path,
        MongoMCPDurableRunStore(),
        dry_run=args.dry_run,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
