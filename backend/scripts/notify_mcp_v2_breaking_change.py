# project/scripts/notify_mcp_v2_breaking_change.py
"""Tâche 18 (v2.0.0) — notifie les clients MCP enregistrés du breaking change.

Compose le message de migration (texte + HTML + blocks Slack) et le diffuse
aux clients MCP inscrits (``core/mcp_client_store``) via les canaux
configurés (email SMTP et/ou webhook Slack). L'envoi est non bloquant :
un échec de canal est loggé, jamais propagé.

Usage :
    venv\\Scripts\\python.exe scripts\\notify_mcp_v2_breaking_change.py [--dry-run]

Options :
    --dry-run   Affiche les clients ciblés et le message composé SANS envoi.

Configuration (variables d'environnement) :
    MCP_NOTIFICATION_SMTP_HOST / _PORT / _USER / _PASSWORD
    MCP_NOTIFICATION_FROM
    MCP_NOTIFICATION_SLACK_WEBHOOK

Références :
    - Guide de migration : docs/mcp/migration/v1-to-v2.md
    - Changelog          : docs/mcp/CHANGELOG.md (section v2.0.0)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.infrastructure.mcp.notifications.notification_service import (  # noqa: E402
    _compose_slack_blocks,
    _compose_text_message,
    build_notification_service,
)

# Sujet + changements majeurs — alignés sur docs/mcp/CHANGELOG.md (v2.0.0)
# et docs/mcp/migration/v1-to-v2.md.
_SUBJECT = "ThinkTuning MCP v2.0.0 — Breaking changes (SamplingPort + orchestrate)"

_BREAKING_CHANGES = [
    "SamplingPort ajouté — nouvelle capacité JSON-RPC 'sampling/create' "
    "(le serveur agit comme client de son propre LLM)",
    "Capacité 'sampling' annoncée à 'initialize' — les clients qui valident "
    "strictement les capacités doivent l'accepter",
    "Version bump 0.1.0 → 2.0.0 (SemVer : breaking changes → major bump)",
    "Tool 'orchestrate' ajouté (additif — découverte via tools/list, "
    "aucun code à changer)",
]

_MIGRATION_GUIDE = "https://github.com/achrafarbounper/ThinkTuning/blob/main/docs/mcp/migration/v1-to-v2.md"


def main() -> int:
    """Point d'entrée du script de notification (retourne un exit code)."""
    # Console Windows : forcer l'UTF-8 (le message contient • et —)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Notifie les clients MCP enregistrés du breaking change v2.0.0.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Affiche les clients ciblés et le message composé sans envoi.",
    )
    args = parser.parse_args()

    if args.dry_run:
        return _dry_run()

    service = build_notification_service()
    if service.email_notifier is None and service.slack_notifier is None:
        print(
            "[!] Aucun canal configuré (MCP_NOTIFICATION_SMTP_* / "
            "MCP_NOTIFICATION_SLACK_WEBHOOK) — rien à envoyer.\n"
            "    Astuce : utilisez --dry-run pour prévisualiser le message."
        )
        return 0

    results = service.notify_all_clients(
        subject=_SUBJECT,
        breaking_changes=_BREAKING_CHANGES,
        migration_guide=_MIGRATION_GUIDE,
    )
    print(
        f"Notifications v2.0.0 envoyées : {results['email']} email(s), "
        f"{results['slack']} message(s) Slack."
    )
    return 0


def _dry_run() -> int:
    """Prévisualise la notification : clients ciblés + message composé."""
    from core.mcp_client_store import get_mcp_client_store

    clients = get_mcp_client_store().list()
    email_targets = [c["client_id"] for c in clients if "@" in c.get("client_id", "")]
    slack_configured = bool(os.getenv("MCP_NOTIFICATION_SLACK_WEBHOOK"))

    print(f"Clients MCP enregistrés : {len(clients)}")
    for client in clients:
        print(f"  - {client['client_id']} (révoqué: {client.get('revoked', False)})")
    print(f"Destinataires email (client_id = adresse) : {email_targets or 'aucun'}")
    print(f"Canal Slack : {'configuré' if slack_configured else 'non configuré'}")
    print()
    print(_compose_text_message(
        breaking_changes=_BREAKING_CHANGES,
        migration_guide=_MIGRATION_GUIDE,
        extra_context={},
    ))
    print()
    print("Blocks Slack :")
    for block in _compose_slack_blocks(
        breaking_changes=_BREAKING_CHANGES,
        migration_guide=_MIGRATION_GUIDE,
        extra_context={},
    ):
        print(f"  - {block['type']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
