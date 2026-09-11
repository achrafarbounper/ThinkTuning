# project/tests/test_mcp_notifications.py
"""Tests du système de notification MCP — email/Slack (tâche 18, v2.0.0).

Valide le contrat de diffusion des breaking changes aux clients enregistrés :
- ``SlackNotifier`` : envoi webhook (succès, statut HTTP non 200, erreur réseau,
  payload JSON avec text + blocks) ;
- ``EmailNotifier`` : envoi SMTP (succès, échec non bloquant, pièces HTML) ;
- builders ``build_email_notifier`` / ``build_slack_notifier`` (config env) ;
- ``NotificationService`` : ciblage email (client_id = adresse), Slack unique,
  compteurs par canal, canaux désactivés, provider injecté vs registre défaut ;
- ``get_mcp_client_store`` : singleton (fallback du service).

Aucun envoi réseau réel : SMTP et webhook sont mockés.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch
from urllib import error as urllib_error

import pytest

from app.infrastructure.mcp.notifications.email_notifier import (
    EmailNotifier,
    build_email_notifier,
)
from app.infrastructure.mcp.notifications.notification_service import (
    NotificationService,
    _compose_slack_blocks,
    _compose_text_message,
)
from app.infrastructure.mcp.notifications.slack_notifier import (
    SlackNotifier,
    build_slack_notifier,
)

# ============================================================================
# Fakes — notificateurs à enregistrement d'appels (sans réseau)
# ============================================================================

class _FakeEmailNotifier:
    """EmailNotifier de test qui enregistre les envois."""

    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.sent: list[dict[str, Any]] = []

    def send(self, to_address: str, subject: str, body_text: str, **kwargs) -> bool:
        self.sent.append({
            "to": to_address, "subject": subject,
            "body_text": body_text, **kwargs,
        })
        return self.success


class _FakeSlackNotifier:
    """SlackNotifier de test qui enregistre les envois."""

    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.sent: list[dict[str, Any]] = []

    def send(self, text: str, *, blocks: list[dict[str, Any]] | None = None) -> bool:
        self.sent.append({"text": text, "blocks": blocks})
        return self.success


def _make_clients(*client_ids: str) -> list[dict]:
    """Fabrique une liste de dicts clients (format ``MCPClientStore.list()``)."""
    return [{"client_id": cid, "revoked": False} for cid in client_ids]


_NOTIFY_KWARGS = {
    "subject": "ThinkTuning MCP v2.0.0 — Breaking changes",
    "breaking_changes": ["SamplingPort ajouté", "Version bump 0.1.0 → 2.0.0"],
    "migration_guide": "docs/mcp/migration/v1-to-v2.md",
}


# ============================================================================
# SlackNotifier — webhook
# ============================================================================

def _patch_urlopen(status: int = 200, *, raises: Exception | None = None):
    """Patch de ``urlopen`` : réponse simulée (context manager avec .status)."""
    if raises is not None:
        return patch(
            "app.infrastructure.mcp.notifications.slack_notifier.urllib_request.urlopen",
            side_effect=raises,
        )
    fake_resp = MagicMock()
    fake_resp.status = status
    fake_resp.__enter__.return_value = fake_resp
    return patch(
        "app.infrastructure.mcp.notifications.slack_notifier.urllib_request.urlopen",
        return_value=fake_resp,
    )


def test_slack_send_success() -> None:
    notifier = SlackNotifier(webhook_url="https://hooks.slack.invalid/t/b/x")
    with _patch_urlopen(status=200):
        assert notifier.send("hello") is True


def test_slack_send_non_200_returns_false() -> None:
    notifier = SlackNotifier(webhook_url="https://hooks.slack.invalid/t/b/x")
    with _patch_urlopen(status=500):
        assert notifier.send("hello") is False


def test_slack_send_url_error_returns_false() -> None:
    notifier = SlackNotifier(webhook_url="https://hooks.slack.invalid/t/b/x")
    with _patch_urlopen(raises=urllib_error.URLError("unreachable")):
        assert notifier.send("hello") is False


def test_slack_send_payload_contains_text_and_blocks() -> None:
    notifier = SlackNotifier(webhook_url="https://hooks.slack.invalid/t/b/x")
    blocks = [{"type": "header", "text": {"type": "plain_text", "text": "H"}}]
    with _patch_urlopen(status=200) as mocked:
        notifier.send("fallback", blocks=blocks)
    request = mocked.call_args.args[0]
    body = request.data.decode("utf-8")
    assert "fallback" in body
    assert "header" in body

# ============================================================================
# EmailNotifier — SMTP
# ============================================================================

def _email_notifier() -> EmailNotifier:
    return EmailNotifier(
        smtp_host="smtp.test", smtp_port=587, smtp_user="u",
        smtp_password="p", from_address="noreply@test.dev",
    )


def test_email_send_success() -> None:
    with patch(
        "app.infrastructure.mcp.notifications.email_notifier.smtplib.SMTP"
    ) as smtp_cls:
        server = smtp_cls.return_value.__enter__.return_value
        assert _email_notifier().send(
            "dev@corp.com", "Sujet", "corps", body_html="<p>corps</p>"
        ) is True
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("u", "p")
        server.send_message.assert_called_once()


def test_email_send_failure_returns_false() -> None:
    with patch(
        "app.infrastructure.mcp.notifications.email_notifier.smtplib.SMTP",
        side_effect=OSError("SMTP down"),
    ):
        assert _email_notifier().send("dev@corp.com", "Sujet", "corps") is False


def test_email_send_attaches_plain_and_html() -> None:
    with patch(
        "app.infrastructure.mcp.notifications.email_notifier.smtplib.SMTP"
    ) as smtp_cls:
        server = smtp_cls.return_value.__enter__.return_value
        _email_notifier().send(
            "dev@corp.com", "Sujet", "texte", body_html="<b>html</b>"
        )
        # 2 parties dans le message : texte brut + HTML ("alternative")
        msg = server.send_message.call_args.args[0]
        assert len(msg.get_payload()) == 2


# ============================================================================
# Builders — configuration via variables d'environnement
# ============================================================================

def test_build_email_notifier_incomplete_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "MCP_NOTIFICATION_SMTP_HOST", "MCP_NOTIFICATION_SMTP_PORT",
        "MCP_NOTIFICATION_SMTP_USER", "MCP_NOTIFICATION_SMTP_PASSWORD",
        "MCP_NOTIFICATION_FROM",
    ):
        monkeypatch.delenv(var, raising=False)
    assert build_email_notifier() is None


def test_build_email_notifier_full_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_NOTIFICATION_SMTP_HOST", "smtp.test")
    monkeypatch.setenv("MCP_NOTIFICATION_SMTP_PORT", "2525")
    monkeypatch.setenv("MCP_NOTIFICATION_SMTP_USER", "u")
    monkeypatch.setenv("MCP_NOTIFICATION_SMTP_PASSWORD", "p")
    monkeypatch.setenv("MCP_NOTIFICATION_FROM", "noreply@test.dev")
    notifier = build_email_notifier()
    assert notifier is not None
    assert notifier.smtp_port == 2525


def test_build_slack_notifier_missing_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_NOTIFICATION_SLACK_WEBHOOK", raising=False)
    assert build_slack_notifier() is None


def test_build_slack_notifier_with_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_NOTIFICATION_SLACK_WEBHOOK", "https://hooks.slack.invalid/t/b/x")
    notifier = build_slack_notifier()
    assert notifier is not None
    assert notifier.webhook_url == "https://hooks.slack.invalid/t/b/x"

# ============================================================================
# NotificationService — orchestration
# ============================================================================

def test_notify_no_clients_returns_zero() -> None:
    service = NotificationService(
        email_notifier=_FakeEmailNotifier(),
        slack_notifier=_FakeSlackNotifier(),
        clients_provider=lambda: [],
    )
    assert service.notify_all_clients(**_NOTIFY_KWARGS) == {"email": 0, "slack": 0}


def test_notify_email_only_to_email_clients() -> None:
    email = _FakeEmailNotifier()
    service = NotificationService(
        email_notifier=email,
        slack_notifier=_FakeSlackNotifier(),
        clients_provider=lambda: _make_clients("dev@corp.com", "cli-001", "ops@corp.com"),
    )
    results = service.notify_all_clients(**_NOTIFY_KWARGS)
    # Seuls les client_id contenant '@' reçoivent l'email (convention v2.0.0)
    assert [s["to"] for s in email.sent] == ["dev@corp.com", "ops@corp.com"]
    assert results["email"] == 2


def test_notify_slack_sent_once() -> None:
    slack = _FakeSlackNotifier()
    service = NotificationService(
        email_notifier=None,
        slack_notifier=slack,
        clients_provider=lambda: _make_clients("dev@corp.com", "cli-001"),
    )
    results = service.notify_all_clients(**_NOTIFY_KWARGS)
    # Slack : 1 seul message (canal global) — email désactivé ici
    assert len(slack.sent) == 1
    assert results == {"email": 0, "slack": 1}


def test_notify_without_channels_never_raises() -> None:
    service = NotificationService(clients_provider=lambda: _make_clients("dev@corp.com"))
    assert service.notify_all_clients(**_NOTIFY_KWARGS) == {"email": 0, "slack": 0}


def test_notify_failed_send_not_counted() -> None:
    service = NotificationService(
        email_notifier=_FakeEmailNotifier(success=False),
        slack_notifier=_FakeSlackNotifier(success=False),
        clients_provider=lambda: _make_clients("dev@corp.com"),
    )
    assert service.notify_all_clients(**_NOTIFY_KWARGS) == {"email": 0, "slack": 0}


def test_notify_email_includes_migration_guide() -> None:
    email = _FakeEmailNotifier()
    service = NotificationService(
        email_notifier=email, clients_provider=lambda: _make_clients("dev@corp.com"),
    )
    service.notify_all_clients(**_NOTIFY_KWARGS)
    assert "docs/mcp/migration/v1-to-v2.md" in email.sent[0]["body_text"]
    assert "SamplingPort ajouté" in email.sent[0]["body_text"]


def test_notify_uses_default_client_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sans provider injecté, le service lit le registre par défaut."""
    fake_store = MagicMock()
    fake_store.list.return_value = _make_clients("dev@corp.com")
    monkeypatch.setattr(
        "core.mcp_client_store.get_mcp_client_store", lambda: fake_store
    )
    email = _FakeEmailNotifier()
    service = NotificationService(email_notifier=email)
    results = service.notify_all_clients(**_NOTIFY_KWARGS)
    fake_store.list.assert_called_once()
    assert results["email"] == 1


# ============================================================================
# Composition des messages
# ============================================================================

def test_compose_text_contains_changes_and_guide() -> None:
    text = _compose_text_message(
        breaking_changes=["Change A", "Change B"],
        migration_guide="docs/mcp/migration/v1-to-v2.md",
        extra_context={},
    )
    assert "Change A" in text and "Change B" in text
    assert "docs/mcp/migration/v1-to-v2.md" in text
    assert "Compatibilité ascendante" in text


def test_compose_slack_blocks_structure() -> None:
    blocks = _compose_slack_blocks(
        breaking_changes=["SamplingPort ajouté"],
        migration_guide="https://example.com/migration",
        extra_context={},
    )
    types = [b["type"] for b in blocks]
    assert types == ["header", "section", "section", "context"]
    assert "SamplingPort ajouté" in blocks[1]["text"]["text"]


# ============================================================================
# get_mcp_client_store — singleton (fallback du service)
# ============================================================================

def test_get_mcp_client_store_is_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    import core.mcp_client_store as store_module

    monkeypatch.setattr(store_module, "_client_store_singleton", None)
    first = store_module.get_mcp_client_store()
    second = store_module.get_mcp_client_store()
    assert first is second
