# project/app/infrastructure/mcp/notifications/notification_service.py
"""Service de notification MCP — orchestrateur email/Slack (tâche 18, v2.0.0).

Orchestre la diffusion des notifications de breaking changes à tous les
clients MCP enregistrés (via ``app/infrastructure/persistence/mcp_client_store``) :

    1. Récupère la liste des clients enregistrés (``MCPClientStore.list()``) ;
    2. Compose le message de migration (texte brut + HTML/Slack blocks) ;
    3. Envoie via les canaux configurés (email, Slack) — non bloquant.

Sécurité : les secrets sont lus depuis les variables d'environnement.
L'envoi est non bloquant (échec loggé, jamais propagé).
"""

from __future__ import annotations

import html
import logging
import re
from collections.abc import Callable
from typing import Any

from app.infrastructure.mcp.notifications.email_notifier import (
    EmailNotifier,
    build_email_notifier,
)
from app.infrastructure.mcp.notifications.slack_notifier import (
    SlackNotifier,
    build_slack_notifier,
)

logger = logging.getLogger("thinktuning.mcp.notifications.service")


class NotificationService:
    """Orchestrateur de notifications MCP (email + Slack)."""

    def __init__(
        self,
        *,
        email_notifier: EmailNotifier | None = None,
        slack_notifier: SlackNotifier | None = None,
        clients_provider: Callable[[], list[dict]] | None = None,
    ) -> None:
        """Construit le service de notification.

        Args:
            email_notifier: notificateur email (``None`` = canal désactivé) ;
            slack_notifier: notificateur Slack (``None`` = canal désactivé) ;
            clients_provider: fournisseur des clients enregistrés (injection
                pour les tests) — ``None`` = registre SQLite par défaut
                (``core.mcp_client_store.get_mcp_client_store().list()``).
        """
        self.email_notifier = email_notifier
        self.slack_notifier = slack_notifier
        self.clients_provider = clients_provider

    def notify_all_clients(
        self,
        *,
        subject: str,
        breaking_changes: list[str],
        migration_guide: str,
        extra_context: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, int]:
        """Notifie tous les clients enregistrés des breaking changes.

        Args:
            subject: sujet de la notification (email) ;
            breaking_changes: liste des changements majeurs ;
            migration_guide: chemin/URL du guide de migration ;
            extra_context: contexte additionnel (optionnel) ;
            correlation_id: identifiant de corrélation MCP (MCP 2.3.0,
                optionnel) — propagé du ``initialize`` (ou de l'en-tête
                ``X-Correlation-Id``) jusqu'aux notifications sortantes :
                journalisé, ajouté aux messages (``extra_context``) et renvoyé
                dans ``extra_context["correlationId"]``. Une valeur hors
                ``[A-Za-z0-9._-]`` (64 max) est NORMALISÉE avant toute
                écriture (même contrat que le serveur MCP).

        Returns:
            Un dict avec le nombre de notifications envoyées par canal
            (``{"email": N, "slack": N}``).
        """
        results = {"email": 0, "slack": 0}
        # MCP 2.3.0 — corrélation : l'identifiant est normalisé (jamais de
        # valeur arbitraire dans un email/Slack) et versé dans le contexte des
        # messages composés (traçabilité de bout en bout : initialize → audit
        # → notification).
        normalized_cid = _normalize_correlation_id(correlation_id)
        if normalized_cid:
            extra_context = {**(extra_context or {}), "correlationId": normalized_cid}

        # Récupérer les clients enregistrés — provider injecté (tests) sinon
        # registre SQLite par défaut (import paresseux — pas de base créée
        # au chargement du module).
        if self.clients_provider is not None:
            clients = self.clients_provider()
        else:
            try:
                from app.infrastructure.persistence.mcp_client_store import get_mcp_client_store

                clients = get_mcp_client_store().list()
            except Exception:  # pragma: no cover — store non disponible
                logger.exception("Impossible de récupérer les clients MCP")
                clients = []

        if not clients:
            logger.info(
                "Aucun client MCP enregistré — notification ignorée (correlation_id=%s)",
                normalized_cid or "-",
            )
            return results

        # Composer le message
        text_body = _compose_text_message(
            breaking_changes=breaking_changes,
            migration_guide=migration_guide,
            extra_context=extra_context or {},
        )
        html_body = _compose_html_message(
            breaking_changes=breaking_changes,
            migration_guide=migration_guide,
            extra_context=extra_context or {},
        )
        slack_blocks = _compose_slack_blocks(
            breaking_changes=breaking_changes,
            migration_guide=migration_guide,
            extra_context=extra_context or {},
        )

        # Envoyer via email (à chaque client avec un email)
        if self.email_notifier:
            for client in clients:
                client_id = client.get("client_id", "")
                # Utiliser le client_id comme email si c'est une adresse email
                # (convention : les clients enregistrés avec un email comme
                # client_id reçoivent la notification).
                if "@" in client_id:
                    success = self.email_notifier.send(
                        to_address=client_id,
                        subject=subject,
                        body_text=text_body,
                        body_html=html_body,
                    )
                    if success:
                        results["email"] += 1

        # Envoyer via Slack (une seule fois — message dans le canal)
        if self.slack_notifier:
            slack_text = f"*{subject}*\n\n" + text_body
            success = self.slack_notifier.send(text=slack_text, blocks=slack_blocks)
            if success:
                results["slack"] += 1

        logger.info(
            "Notifications envoyées : %d email(s), %d Slack (correlation_id=%s)",
            results["email"],
            results["slack"],
            normalized_cid or "-",
        )
        return results


def _normalize_correlation_id(value: str | None) -> str:
    """Normalise un ``correlation_id`` destiné aux notifications (MCP 2.3.0).

    Même contrat que le serveur MCP (``mcp_server.resolve_correlation_id``) :
    borné à 64 caractères ``[A-Za-z0-9._-]``. Un identifiant ABSENT ou
    entièrement invalide retourne ``""`` (aucune corrélation inventée côté
    notification : c'est à l'appelant de la fournir).
    """
    candidate = re.sub(r"[^A-Za-z0-9._-]", "", str(value or "").strip())[:64]
    return candidate


def build_notification_service() -> NotificationService:
    """Construit un ``NotificationService`` avec les notificateurs configurés.

    Returns:
        Un ``NotificationService`` prêt à l'utilisation (les canaux non
        configurés sont ``None`` — le service les ignore).
    """
    email_notifier = build_email_notifier()
    slack_notifier = build_slack_notifier()
    return NotificationService(
        email_notifier=email_notifier,
        slack_notifier=slack_notifier,
    )


def _compose_text_message(
    *,
    breaking_changes: list[str],
    migration_guide: str,
    extra_context: dict[str, Any],
) -> str:
    """Compose le corps du message (texte brut).

    MCP 2.3.0 — corrélation : ``extra_context["correlationId"]`` (quand
    présent) est rendu dans le corps — le destinataire peut rattacher la
    notification à la requête MCP qui l'a déclenchée (logs + audit).
    """
    lines = [
        "ThinkTuning MCP — Notification de mise à jour majeure",
        "=" * 50,
        "",
        "Des breaking changes ont été introduits dans la surface MCP.",
        "",
        "Changements majeurs :",
    ]
    for change in breaking_changes:
        lines.append(f"  • {change}")
    lines += [
        "",
        f"Guide de migration : {migration_guide}",
        "",
        "Action requise : mettez à jour votre client MCP pour accepter",
        "la nouvelle capacité 'sampling' et la version '2.0.0'.",
        "",
        "Compatibilité ascendante : les clients v1.x continuent de fonctionner.",
    ]
    correlation_id = str(extra_context.get("correlationId") or "").strip()
    if correlation_id:
        lines += ["", f"Correlation ID : {correlation_id}"]
    lines += ["", "Support : https://github.com/achrafarbounper/ThinkTuning/issues"]
    return "\n".join(lines)


def _compose_html_message(
    *,
    breaking_changes: list[str],
    migration_guide: str,
    extra_context: dict[str, Any],
) -> str:
    """Compose le corps du message (HTML).

    MCP 2.3.0 — corrélation : ``extra_context["correlationId"]`` est rendu
    (échappé) quand présent — même traçabilité que la version texte.
    """
    changes_html = "".join(f"<li>{c}</li>" for c in breaking_changes)
    correlation_id = str(extra_context.get("correlationId") or "").strip()
    correlation_html = (
        f"<p><em>Correlation ID : {html.escape(correlation_id)}</em></p>"
        if correlation_id
        else ""
    )
    return f"""
    <html>
    <body>
        <h2>ThinkTuning MCP — Notification de mise à jour majeure</h2>
        <p>Des breaking changes ont été introduits dans la surface MCP.</p>
        <h3>Changements majeurs :</h3>
        <ul>{changes_html}</ul>
        <p><strong>Guide de migration :</strong>
            <a href="{migration_guide}">{migration_guide}</a>
        </p>
        <p>Action requise : mettez à jour votre client MCP pour accepter
        la nouvelle capacité 'sampling' et la version '2.0.0'.</p>
        <p>
        <em>
        Compatibilité ascendante : les clients v1.x continuent de fonctionner.
        </em>
        </p>
        {correlation_html}
        <hr>
        <p>Support :
        <a href="https://github.com/achrafarbounper/ThinkTuning/issues">GitHub Issues</a>
        </p>
    </body>
    </html>
    """


def _compose_slack_blocks(
    *,
    breaking_changes: list[str],
    migration_guide: str,
    extra_context: dict[str, Any],
) -> list[dict[str, Any]]:
    """Compose les blocks Slack structurés.

    MCP 2.3.0 — corrélation : ``extra_context["correlationId"]`` est ajouté au
    block ``context`` quand présent (pas de nouveau block — la structure
    ``header/section/section/context`` reste le contrat des tests existants).
    """
    changes_text = "\n".join(f"• {c}" for c in breaking_changes)
    compat_text = "Compatibilité ascendante : les clients v1.x continuent de fonctionner."
    correlation_id = str(extra_context.get("correlationId") or "").strip()
    if correlation_id:
        compat_text += f"\nCorrelation ID : {correlation_id}"
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "ThinkTuning MCP — Mise à jour majeure"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Breaking changes introduits :*\n{changes_text}",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"<{migration_guide}|Guide de migration>\n\nAction requise :"
                + " mettez à jour votre client MCP.",
            },
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": compat_text}],
        },
    ]
