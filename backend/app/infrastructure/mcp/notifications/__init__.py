# project/app/infrastructure/mcp/notifications/__init__.py
"""Notifications MCP — email/Slack aux clients enregistrés (tâche 18, v2.0.0).

Système de notification pour informer les clients MCP enregistrés des
breaking changes (v1.x → v2.0.0) :

- ``EmailNotifier`` : envoi via SMTP (configurable via variables d'environnement) ;
- ``SlackNotifier`` : envoi via webhook Slack (configurable) ;
- ``NotificationService`` : orchestrateur qui compose le message de migration
  et le diffuse à tous les clients enregistrés via les canaux configurés.

Sécurité : les secrets (SMTP password, webhook URL) sont lus depuis les
variables d'environnement — jamais en clair dans le code.

Utilisation :

    service = NotificationService()
    service.notify_all_clients(
        subject="ThinkTuning MCP v2.0.0 — Breaking Changes",
        breaking_changes=["SamplingPort ajouté", "orchestrate tool ajouté"],
        migration_guide="docs/mcp/migration/v1-to-v2.md",
    )
"""

from __future__ import annotations

from app.infrastructure.mcp.notifications.email_notifier import (
    EmailNotifier,
    build_email_notifier,
)
from app.infrastructure.mcp.notifications.notification_service import (
    NotificationService,
    build_notification_service,
)
from app.infrastructure.mcp.notifications.slack_notifier import (
    SlackNotifier,
    build_slack_notifier,
)

__all__ = [
    "EmailNotifier",
    "NotificationService",
    "SlackNotifier",
    "build_email_notifier",
    "build_notification_service",
    "build_slack_notifier",
]
