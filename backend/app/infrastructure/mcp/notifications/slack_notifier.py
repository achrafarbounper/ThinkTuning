# project/app/infrastructure/mcp/notifications/slack_notifier.py
"""Notification Slack aux clients MCP enregistrés (tâche 18, v2.0.0).

Envoi de messages via webhook Slack pour informer les clients des breaking
changes. Configuration via variable d'environnement :

    MCP_NOTIFICATION_SLACK_WEBHOOK — URL du webhook Slack

Sécurité : l'URL du webhook est lue depuis les variables d'environnement —
jamais en clair dans le code. L'envoi est non bloquant (échec loggé, jamais
propagé) pour ne pas impacter le démarrage du serveur.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

logger = logging.getLogger("thinktuning.mcp.notifications.slack")


class SlackNotifier:
    """Notifier Slack pour les clients MCP (webhook)."""

    def __init__(self, *, webhook_url: str) -> None:
        self.webhook_url = webhook_url

    def send(self, text: str, *, blocks: list[dict[str, Any]] | None = None) -> bool:
        """Envoie un message Slack (non bloquant — échec loggé, jamais propagé).

        Args:
            text: texte du message (fallback) ;
            blocks: blocks Slack structurés (optionnel) ;

        Returns:
            ``True`` si l'envoi a réussi, ``False`` sinon.
        """
        try:
            payload: dict[str, Any] = {"text": text}
            if blocks:
                payload["blocks"] = blocks

            data = json.dumps(payload).encode("utf-8")
            req = urllib_request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib_request.urlopen(req, timeout=10) as response:
                if response.status == 200:
                    logger.info("Message Slack envoyé avec succès")
                    return True
                logger.warning("Slack a répondu avec le statut %d", response.status)
                return False
        except (urllib_error.URLError, OSError) as exc:
            logger.exception("Échec envoi Slack : %s", exc)
            return False


def build_slack_notifier() -> SlackNotifier | None:
    """Construit un ``SlackNotifier`` depuis les variables d'environnement.

    Returns:
        Un ``SlackNotifier`` configuré, ou ``None`` si l'URL du webhook
        n'est pas définie.
    """
    webhook_url = os.getenv("MCP_NOTIFICATION_SLACK_WEBHOOK", "").strip()
    if not webhook_url:
        logger.info(
            "Webhook Slack non configuré — notifications Slack désactivées "
            "(définir MCP_NOTIFICATION_SLACK_WEBHOOK)"
        )
        return None
    return SlackNotifier(webhook_url=webhook_url)
