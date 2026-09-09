# project/app/infrastructure/mcp/notifications/email_notifier.py
"""Notification email aux clients MCP enregistrés (tâche 18, v2.0.0).

Envoi d'emails via SMTP pour informer les clients des breaking changes.
Configuration via variables d'environnement :

    MCP_NOTIFICATION_SMTP_HOST     — hôte SMTP (ex. smtp.gmail.com)
    MCP_NOTIFICATION_SMTP_PORT     — port SMTP (ex. 587)
    MCP_NOTIFICATION_SMTP_USER     — utilisateur SMTP
    MCP_NOTIFICATION_SMTP_PASSWORD — mot de passe SMTP
    MCP_NOTIFICATION_FROM          — adresse expéditeur

Sécurité : les secrets sont lus depuis les variables d'environnement —
jamais en clair dans le code. L'envoi est non bloquant (échec loggé, jamais
propagé) pour ne pas impacter le démarrage du serveur.
"""

from __future__ import annotations

import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger("thinktuning.mcp.notifications.email")


class EmailNotifier:
    """Notifier email pour les clients MCP (SMTP)."""

    def __init__(
        self,
        *,
        smtp_host: str,
        smtp_port: int,
        smtp_user: str,
        smtp_password: str,
        from_address: str,
        use_tls: bool = True,
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.from_address = from_address
        self.use_tls = use_tls

    def send(
        self,
        to_address: str,
        subject: str,
        body_text: str,
        *,
        body_html: str | None = None,
    ) -> bool:
        """Envoie un email (non bloquant — échec loggé, jamais propagé).

        Args:
            to_address: adresse du destinataire ;
            subject: sujet de l'email ;
            body_text: corps du message (texte brut) ;
            body_html: corps du message (HTML, optionnel) ;

        Returns:
            ``True`` si l'envoi a réussi, ``False`` sinon.
        """
        try:
            msg = MIMEMultipart("alternative")
            msg["From"] = self.from_address
            msg["To"] = to_address
            msg["Subject"] = subject
            msg.attach(MIMEText(body_text, "plain", "utf-8"))
            if body_html:
                msg.attach(MIMEText(body_html, "html", "utf-8"))

            with smtplib.SMTP(self.smtp_host, self.smtp_port) as server:
                if self.use_tls:
                    server.starttls()
                server.login(self.smtp_user, self.smtp_password)
                server.send_message(msg)

            logger.info("Email envoyé à %s (sujet: %s)", to_address, subject)
            return True
        except Exception:  # pragma: no cover — SMTP non disponible en test
            logger.exception("Échec envoi email à %s", to_address)
            return False


def build_email_notifier() -> EmailNotifier | None:
    """Construit un ``EmailNotifier`` depuis les variables d'environnement.

    Returns:
        Un ``EmailNotifier`` configuré, ou ``None`` si la configuration SMTP
        est incomplète (variables d'environnement manquantes).
    """
    smtp_host = os.getenv("MCP_NOTIFICATION_SMTP_HOST", "").strip()
    smtp_port_str = os.getenv("MCP_NOTIFICATION_SMTP_PORT", "587").strip()
    smtp_user = os.getenv("MCP_NOTIFICATION_SMTP_USER", "").strip()
    smtp_password = os.getenv("MCP_NOTIFICATION_SMTP_PASSWORD", "")
    from_address = os.getenv("MCP_NOTIFICATION_FROM", "").strip()

    if not all([smtp_host, smtp_user, smtp_password, from_address]):
        logger.info(
            "Configuration SMTP incomplète — notifications email désactivées "
            "(définir MCP_NOTIFICATION_SMTP_HOST/USER/FROM/PASSWORD)"
        )
        return None

    try:
        smtp_port = int(smtp_port_str)
    except ValueError:
        logger.warning("MCP_NOTIFICATION_SMTP_PORT invalide — repli sur 587")
        smtp_port = 587

    return EmailNotifier(
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_password=smtp_password,
        from_address=from_address,
    )
