"""Tests du pilotage runtime de la politique SSRF (bac à sable, SCRUM-139).

La page Paramètres du dashboard persiste ``ssrf_enabled`` / ``ssrf_allowlist``
(module de configuration IHM) : ces clés SURCLASSENT l'environnement dès la
première sauvegarde, via ``set_runtime_network_policy`` /
``apply_persisted_network_policy`` — sans redémarrage, l'env restant le repli
pour toute clé non persistée. Sémantique fail-closed conservée (défaut ON).
"""

from __future__ import annotations

import pytest

from ia.tools import sandbox


@pytest.fixture(autouse=True)
def _reset_runtime_policy():
    """Aucun report d'override runtime entre les tests."""
    sandbox.reset_runtime_network_policy()
    yield
    sandbox.reset_runtime_network_policy()


# --- ssrf_protection_enabled (priorité runtime > env > défaut) ----------------


def test_protection_on_by_default(monkeypatch):
    """Fail-closed : sans env ni override runtime, la protection est ACTIVE."""
    monkeypatch.delenv("AGENT_BLOCK_PRIVATE_HOSTS", raising=False)
    assert sandbox.ssrf_protection_enabled() is True


def test_env_off_without_runtime_override(monkeypatch):
    """L'env historique désactive toujours (comportement inchangé)."""
    monkeypatch.setenv("AGENT_BLOCK_PRIVATE_HOSTS", "0")
    assert sandbox.ssrf_protection_enabled() is False


def test_runtime_override_beats_env(monkeypatch):
    """La page Paramètres surclasse l'env (base dit ON, env dit 0)."""
    monkeypatch.setenv("AGENT_BLOCK_PRIVATE_HOSTS", "0")
    sandbox.set_runtime_network_policy(ssrf_enabled=True)
    assert sandbox.ssrf_protection_enabled() is True


def test_runtime_override_none_returns_to_env(monkeypatch):
    """``None`` (clé non persistée) rend la main à l'environnement."""
    monkeypatch.setenv("AGENT_BLOCK_PRIVATE_HOSTS", "0")
    sandbox.set_runtime_network_policy(ssrf_enabled=True)
    sandbox.reset_runtime_network_policy()
    assert sandbox.ssrf_protection_enabled() is False


# --- allowlist (priorité runtime > env) ----------------------------------------


def test_allowlist_from_env(monkeypatch):
    monkeypatch.setenv("AGENT_PRIVATE_HOST_ALLOWLIST", "localhost, searxng")
    assert sandbox.get_private_host_allowlist() == {"localhost", "searxng"}


def test_runtime_allowlist_replaces_env(monkeypatch):
    """Une allowlist persistée REMPLACE l'env (la base est la vérité)."""
    monkeypatch.setenv("AGENT_PRIVATE_HOST_ALLOWLIST", "searxng")
    sandbox.set_runtime_network_policy(ssrf_allowlist="127.0.0.1,localhost")
    assert sandbox.get_private_host_allowlist() == {"127.0.0.1", "localhost"}


def test_runtime_allowlist_empty_blocks_everything(monkeypatch):
    """Une allowlist persistée VIDE neutralise l'env (ex. reset côté IHM)."""
    monkeypatch.setenv("AGENT_PRIVATE_HOST_ALLOWLIST", "searxng")
    sandbox.set_runtime_network_policy(ssrf_allowlist="")
    assert sandbox.get_private_host_allowlist() == set()


# --- apply_persisted_network_policy (valeurs brutes du store IHM) --------------


def test_apply_persisted_coerces_strings(monkeypatch):
    """Booléens stockés en chaîne (JSON legacy) : coercition tolérante."""
    monkeypatch.delenv("AGENT_BLOCK_PRIVATE_HOSTS", raising=False)
    monkeypatch.delenv("AGENT_PRIVATE_HOST_ALLOWLIST", raising=False)
    sandbox.apply_persisted_network_policy(
        {"ssrf_enabled": "false", "ssrf_allowlist": "searxng"}
    )
    assert sandbox.ssrf_protection_enabled() is False
    assert sandbox.get_private_host_allowlist() == {"searxng"}


def test_apply_persisted_missing_keys_fall_back_to_env(monkeypatch):
    """Clés absentes de la base : l'env reprend la main (override ``None``)."""
    monkeypatch.setenv("AGENT_BLOCK_PRIVATE_HOSTS", "0")
    monkeypatch.setenv("AGENT_PRIVATE_HOST_ALLOWLIST", "searxng")
    sandbox.apply_persisted_network_policy({})
    assert sandbox.ssrf_protection_enabled() is False
    assert sandbox.get_private_host_allowlist() == {"searxng"}


# --- enforce_host_policy (bascule IHM de bout en bout) -------------------------


def test_enforce_allows_loopback_when_runtime_disabled():
    """Désactiver la protection via l'IHM autorise immédiatement le loopback."""
    sandbox.set_runtime_network_policy(ssrf_enabled=False)
    sandbox.enforce_host_policy("http://127.0.0.1:8888/search")  # ne lève pas


def test_enforce_allows_searxng_via_runtime_allowlist():
    """Scénario SearXNG : protection ON + allowlist persistée (page Paramètres)."""
    sandbox.set_runtime_network_policy(
        ssrf_enabled=True, ssrf_allowlist="searxng,127.0.0.1"
    )
    sandbox.enforce_host_policy("http://searxng:8080/search")  # allowlisté
    sandbox.enforce_host_policy("http://127.0.0.1:8888/search")  # allowlisté


def test_enforce_blocks_private_host_without_allowlist():
    """Protection ON + allowlist vide : le loopback reste interdit (fail-closed)."""
    sandbox.set_runtime_network_policy(ssrf_enabled=True, ssrf_allowlist="")
    with pytest.raises(PermissionError) as excinfo:
        sandbox.enforce_host_policy("http://127.0.0.1:8888/search")
    assert "SSRF" in str(excinfo.value)
