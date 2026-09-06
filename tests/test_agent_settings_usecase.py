"""Tests du use case des paramètres de l'agent (app/application/agent_settings_usecase).

Vérifient :
    - ``get_effective_settings`` fusionne base > env > défauts ;
    - ``validate_settings`` rejette les valeurs hors bornes et les clés vides ;
    - ``update_settings`` persiste uniquement les clés connues ;
    - le port est mocké (aucune I/O SQLite).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.application import agent_settings_usecase as uc


class FakeSettingsPort:
    """Port en mémoire pour les tests."""

    def __init__(self, persisted: dict[str, Any] | None = None) -> None:
        self._persisted: dict[str, Any] = dict(persisted or {})
        self.written: list[dict[str, Any]] = []

    def get_all(self) -> dict[str, Any]:
        return dict(self._persisted)

    def save_many(self, values: dict[str, Any]) -> dict[str, Any]:
        self.written.append(dict(values))
        self._persisted.update(values)
        return values


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    """Réinitialise le cache de get_settings() avant chaque test."""
    from app.config.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- get_effective_settings ------------------------------------------------


def test_defaults_when_empty_port_and_no_env():
    """Sans base ni env : renvoie les défauts (Settings)."""
    port = FakeSettingsPort()
    result = uc.get_effective_settings(port)
    assert result["provider"] == "ollama"
    # Settings a "openrouter/free" comme défaut pour agent_model_name.
    assert result["model"] == "openrouter/free"
    assert result["timeout_seconds"] == 600  # Settings default


def test_persisted_overrides_default():
    """Une valeur persistée écrase le défaut."""
    port = FakeSettingsPort({"provider": "openrouter"})
    result = uc.get_effective_settings(port)
    assert result["provider"] == "openrouter"


def test_env_overrides_default(monkeypatch):
    """Une variable d'environnement écrase le défaut."""
    monkeypatch.setenv("AGENT_PROVIDER", "ollama")
    monkeypatch.setenv("AGENT_MODEL_NAME", "llama3.1:8b")
    port = FakeSettingsPort()
    result = uc.get_effective_settings(port)
    assert result["provider"] == "ollama"
    assert result["model"] == "llama3.1:8b"


def test_persisted_overrides_env(monkeypatch):
    """La base est prioritaire sur l'environnement."""
    monkeypatch.setenv("AGENT_PROVIDER", "ollama")
    port = FakeSettingsPort({"provider": "openrouter"})
    result = uc.get_effective_settings(port)
    assert result["provider"] == "openrouter"


# --- validate_settings -----------------------------------------------------


def test_validate_valid_empty():
    """Un dict vide ne produit aucune erreur."""
    errors = uc.validate_settings({})
    assert errors == []


def test_validate_invalid_provider():
    errors = uc.validate_settings({"provider": "toaster"})
    assert any("provider" in e for e in errors)


def test_validate_timeout_out_of_range():
    errors = uc.validate_settings({"timeout_seconds": 5})
    assert any("timeout_seconds" in e for e in errors)


def test_validate_timeout_coerced_to_float():
    """Un timeout valide est converti en float."""
    values = {"timeout_seconds": "30.5"}
    errors = uc.validate_settings(values)
    assert errors == []
    assert values["timeout_seconds"] == 30.5


def test_validate_context_length_coerced_to_int():
    values = {"context_length": "4096"}
    errors = uc.validate_settings(values)
    assert errors == []
    assert values["context_length"] == 4096


def test_validate_temperature_out_of_range():
    errors = uc.validate_settings({"temperature": 3.0})
    assert any("temperature" in e for e in errors)


def test_validate_openrouter_empty_key_rejected():
    errors = uc.validate_settings({"provider": "openrouter", "openrouter_api_key": ""})
    assert any("openrouter_api_key" in e for e in errors)


def test_validate_openrouter_missing_key_ok():
    """Une clé absente (None) n'est pas rejetée (pas de sauvegarde explicite)."""
    errors = uc.validate_settings({"provider": "openrouter", "openrouter_api_key": None})
    assert errors == []


# --- update_settings -------------------------------------------------------


def test_update_persists_known_keys_only():
    """Seules les clés SETTING_KEYS sont écrites."""
    port = FakeSettingsPort()
    effective, errors, written = uc.update_settings(port, {
        "provider": "hf",
        "unknown_key": "ignored",
    })
    assert errors == []
    assert "provider" in written
    assert "unknown_key" not in written
    assert port._persisted["provider"] == "hf"
    assert "unknown_key" not in port._persisted


def test_update_validation_error_blocks_write():
    """Une erreur de validation empêche l'écriture."""
    port = FakeSettingsPort()
    effective, errors, written = uc.update_settings(port, {"provider": "toaster"})
    assert len(errors) > 0
    assert written == []
    assert port._persisted == {}


def test_update_returns_effective_config():
    """Le 3e élément renvoie les clés effectivement écrites."""
    port = FakeSettingsPort({"model": "qwen2.5"})
    effective, errors, written = uc.update_settings(port, {"provider": "ollama"})
    assert errors == []
    assert written == ["provider"]
    assert effective["provider"] == "ollama"
    assert effective["model"] == "qwen2.5"  # valeur persistée préservée
