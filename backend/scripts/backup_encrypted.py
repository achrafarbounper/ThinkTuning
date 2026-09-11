#!/usr/bin/env python
"""Encrypted Fernet export/import of the MongoDB persistence database."""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logger = logging.getLogger("backup_encrypted")
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "experiments/backups"))
COLLECTIONS = ("agent_sessions", "agent_session_messages", "agent_runs", "agent_flows",
               "agent_approvals", "agent_audit", "agent_settings", "jobs", "train_metrics",
               "scheduled_jobs", "mcp_clients", "service_accounts", "revoked_tokens",
               "copilot_feedback", "tool_audit")


def _fernet():
    from cryptography.fernet import Fernet
    raw = (os.getenv("BACKUP_ENCRYPTION_KEY") or os.getenv("STORE_ENCRYPTION_KEY") or "").strip()
    if not raw:
        raise RuntimeError("BACKUP_ENCRYPTION_KEY ou STORE_ENCRYPTION_KEY est requis")
    return Fernet(raw)


def make_backup() -> dict:
    from app.infrastructure.persistence.mongodb import get_mongo_provider
    provider = get_mongo_provider()
    exported = {}
    for name in COLLECTIONS:
        exported[name] = list(provider.collection(name).find({}, {"_id": 0}))
    raw = json.dumps(exported, ensure_ascii=False, default=str).encode()
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w:gz") as tar:
        info = tarfile.TarInfo("mongodb.json")
        info.size = len(raw)
        tar.addfile(info, io.BytesIO(raw))
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = BACKUP_DIR / f"thinktuning-{timestamp}.mongo.backup.enc"
    out.write_bytes(_fernet().encrypt(blob.getvalue()))
    manifest = {"created_at": timestamp, "archive": out.name,
                "collections": {k: len(v) for k, v in exported.items()}}
    (BACKUP_DIR / f"{out.stem}.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"archive": str(out), "manifest": manifest}


def restore_backup(archive: str, *, apply: bool = False) -> dict:
    from app.infrastructure.persistence.mongodb import get_mongo_provider
    try:
        plain = _fernet().decrypt(Path(archive).read_bytes())
        with tarfile.open(fileobj=io.BytesIO(plain), mode="r:gz") as tar:
            data = json.loads(tar.extractfile("mongodb.json").read())
    except Exception as exc:
        raise RuntimeError("échec de déchiffrement ou archive MongoDB invalide") from exc
    if apply:
        db = get_mongo_provider()
        for name, rows in data.items():
            c = db.collection(name)
            c.delete_many({})
            if rows:
                c.insert_many(rows)
    return {"collections": {k: len(v) for k, v in data.items()}, "applied": apply}


def main() -> int:
    parser = argparse.ArgumentParser(description="Backup/restauration MongoDB chiffré")
    parser.add_argument("--restore", metavar="ARCHIVE")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        result = restore_backup(args.restore, apply=args.apply) if args.restore else make_backup()
    except Exception as exc:
        print(f"[ERREUR] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
