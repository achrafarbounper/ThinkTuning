#!/usr/bin/env python
"""Purge TTL des données personnelles — P2 lot 16 (Privacy/Résilience).

Nettoie les stores contenant des données personnelles / conversationnelles :

    - ``agent_sessions``  (messages + sessions, TTL par défaut 90 jours) ;
    - ``agent_audit``     (journal d'audit, TTL par défaut 365 jours).

Usage (cron hebdomadaire) :
    python scripts/retention.py --days-sessions 90 --days-audit 365
    python scripts/retention.py --dry-run            # simulation (rien n'est supprimé)

Variables d'environnement (repli) : RETENTION_SESSIONS_DAYS, RETENTION_AUDIT_DAYS.
Les dates sont comparées en ISO UTC (millisecondes) — colonnes des stores.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw.strip() else default
    except ValueError:
        return default


def main() -> int:
    parser = argparse.ArgumentParser(description="Purge TTL sessions + audit")
    parser.add_argument("--days-sessions", type=int, default=None, help="TTL sessions (défaut 90)")
    parser.add_argument("--days-audit", type=int, default=None, help="TTL audit (défaut 365)")
    parser.add_argument("--dry-run", action="store_true", help="Simule sans supprimer")
    args = parser.parse_args()

    days_sessions = args.days_sessions or _parse_int_env("RETENTION_SESSIONS_DAYS", 90)
    days_audit = args.days_audit or _parse_int_env("RETENTION_AUDIT_DAYS", 365)

    from core.audit_store import get_audit_store
    from core.session_store import get_session_store

    if args.dry_run:
        # En dry-run, on simule sur un store jetable sans déranger le store partagé.
        import tempfile

        from core.audit_store import AuditStore
        from core.session_store import SessionStore

        tmp = tempfile.mkdtemp(prefix="tt_retention_dry_")
        audit = AuditStore(str(Path(tmp) / "audit.db"))
        sessions = SessionStore(str(Path(tmp) / "sessions.db"))
        print(
            "DRY-RUN : aucun store touché "
            f"(sessions TTL {days_sessions}j, audit TTL {days_audit}j)."
        )
        return 0

    sessions = get_session_store().purge_expired(days_sessions)
    audit = get_audit_store().prune_older_than(days_audit)
    print(
        f"TTL appliqué : sessions {sessions}, audit {audit} "
        f"(sessions {days_sessions}j / audit {days_audit}j)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
