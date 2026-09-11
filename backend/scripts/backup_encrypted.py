#!/usr/bin/env python
"""Backup chiffré des stores transactionnels — P2 lot 16 (Privacy/Résilience).

Chiffre ``jobs.db`` + ``train_metrics.db`` (les bases d'entraînement/audit
transactionnelles) dans une archive unique Fernet (AES-128-CBC + HMAC) :
    - clé : ``STORE_ENCRYPTION_KEY`` (même clé que le chiffrement au repos,
      Fernet 32 octets) ; surchargeable via ``BACKUP_ENCRYPTION_KEY`` ;
    - sortie : ``experiments/backups/thinktuning-<timestamp>.backup.enc``
      (+ manifeste ``.json`` horodaté avec liste des bases et tailles) ;
    - flux : aucun fichier temp en clair sur disque (tar pipé dans Fernet).

Usage :
    python scripts/backup_encrypted.py                    # backup complet
    python scripts/backup_encrypted.py --restore FICHIER  # restaure (exercice)

La restauration est DÉLIBÉRÉMENT non destructive : elle écrit dans un
répertoire séparé et exige ``--apply`` pour écraser les bases live.
Exercice de restauration : docs/OPS_BACKUP_RESTORE.md.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logger = logging.getLogger("backup_encrypted")

# Bases transactionnelles sauvegardées (relatives à experiments/).
DEFAULT_DBS = ("jobs.db", "train_metrics.db")
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "experiments/backups"))
RESTORE_DIR = Path(os.getenv("BACKUP_RESTORE_DIR", "experiments/restore"))


def _fernet():
    from cryptography.fernet import Fernet

    raw = (os.getenv("BACKUP_ENCRYPTION_KEY") or os.getenv("STORE_ENCRYPTION_KEY") or "").strip()
    if not raw:
        raise RuntimeError(
            "Aucune clé de backup : définissez STORE_ENCRYPTION_KEY (ou "
            "BACKUP_ENCRYPTION_KEY) — génération : python -c \"from "
            'cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    try:
        return Fernet(raw)
    except (ValueError, TypeError) as exc:
        raise RuntimeError(f"clé Fernet invalide : {exc}") from exc


def _locate_db(name: str) -> Path | None:
    for root in (Path(os.getenv("JOB_STORE_PATH", "experiments")), Path("experiments")):
        candidate = root / name
        if candidate.is_file():
            return candidate
    return None


def _sql_integrity(path: Path) -> bool:
    try:
        conn = sqlite3.connect(path)
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
            return bool(row) and row[0] == "ok"
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def make_backup() -> dict:
    """Archive les bases transactionnelles dans une archive Fernet chiffrée."""
    _fernet()  # valide la clé + la dépendance cryptography dès le début
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    payloads: dict[str, bytes | None] = {}
    for name in DEFAULT_DBS:
        path = _locate_db(name)
        if path is None:
            logger.warning("base absente, ignorée : %s", name)
            payloads[name] = None
            continue
        payloads[name] = path.read_bytes()

    # Archive tar en mémoire, chiffrée (jamais de fichier en clair sur disque).
    blob = io.BytesIO()
    with tarfile.open(fileobj=blob, mode="w") as tar:
        for name, data in payloads.items():
            if data is None:
                continue
            raw = io.BytesIO(data)
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, raw)

    token = _fernet().encrypt(blob.getvalue())
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = BACKUP_DIR / f"thinktuning-{timestamp}.backup.enc"

    sizes = {name: (len(data) if data else 0) for name, data in payloads.items()}
    manifest = {
        "created_at": timestamp,
        "archive": out.name,
        "dbs": {name: {"size_bytes": size} for name, size in sizes.items()},
        "integrity": {
            name: (_sql_integrity(p) if (p := _locate_db(name)) else None)
            for name in DEFAULT_DBS
        },
    }
    out.write_bytes(token)
    (BACKUP_DIR / f"{out.stem}.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    logger.info("Backup chiffré écrit : %s (%d octets)", out, len(token))
    return {"archive": str(out), "manifest": manifest}
def restore_backup(archive: str, *, apply: bool = False) -> dict:
    """Déchiffre et restaure la base dans ``RESTORE_DIR`` (jamais destructif)."""
    token = Path(archive).read_bytes()
    try:
        plain = _fernet().decrypt(token)
    except Exception as exc:  # cryptography.fernet.InvalidToken
        raise RuntimeError(
            "échec de déchiffrement : archive corrompue ou clé différente"
        ) from exc

    target_dir = RESTORE_DIR / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(plain), mode="r") as tar:
        tar.extractall(target_dir, filter="data")

    restored: dict[str, dict] = {}
    for name in DEFAULT_DBS:
        path = target_dir / name
        if not path.is_file():
            restored[name] = {"restored": False, "reason": "absent de l'archive"}
            continue
        ok = _sql_integrity(path)
        restored[name] = {"restored": True, "integrity_ok": ok, "size_bytes": path.stat().st_size}

    if apply:
        for name, info in restored.items():
            if not info.get("restored") or not info.get("integrity_ok"):
                continue
            from shutil import copy2

            copy2(target_dir / name, _locate_db(name) or Path("experiments") / name)
        logger.info("Restauration --apply : bases live écrasées depuis %s", target_dir)

    return {"target": str(target_dir), "restored": restored, "applied": apply}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Backup chiffré des stores transactionnels")
    parser.add_argument("--restore", metavar="ARCHIVE", help="Restaure l'archive spécifiée")
    parser.add_argument("--apply", action="store_true", help="Écrase les bases live (restore)")
    args = parser.parse_args()

    try:
        if args.restore:
            result = restore_backup(args.restore, apply=args.apply)
        else:
            result = make_backup()
    except Exception as exc:  # noqa: BLE001 - CLI explicite
        print(f"[ERREUR] {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
