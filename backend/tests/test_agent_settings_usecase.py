"""Tests du use case des paramètres de l'agent (app/application/agent_settings_usecase).

Vérifient :
    - ``get_effective_settings`` fusionne base > env > défauts ;
    - ``validate_settings`` rejette les valeurs hors bornes et les clés vides ;
    - ``update_settings`` persiste uniquement les clés connues ;
    - le port est mocké (aucune I/O SQLite/MongoDB) ;
    - les réglages déplacés de ``app/config/settings.py`` (SCRUM-138) sont des
      clés du module IHM à part entière.
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
def _clean_agent_env(monkeypatch):
    """Environnement agent / MCP nettoyé (le use case n'utilise plus Settings).

    SCRUM-138 : la couche env + défauts vient de ``core.agent_settings`` (la
    base reste prioritaire) ; on retire les variables qui pollueraient les
    défauts attendus (un ``.env`` machine ne doit pas faire flakker les tests).
    """
    for key in (
        "AGENT_PROVIDER",
        "AGENT_MODEL_NAME",
        "AGENT_OLLAMA_URL",
        "AGENT_OPENROUTER_URL",
        "AGENT_HF_URL",
        "AGENT_LM_STUDIO_URL",
        "AGENT_TIMEOUT_SECONDS",
        "AGENT_CONTEXT_LENGTH",
        "AGENT_MAX_LLM_ROUNDS",
        "AGENT_MAX_TOOL_CALLS",
        "AGENT_LOG_LEVEL",
        "OPENROUTER_API_KEY",
        "HF_API_KEY",
        "HF_TOKEN",
        "MCP_FIRST",
        "MCP_AUTH_REQUIRED",
    ):
        monkeypatch.delenv(key, raising=False)
    yield


# --- get_effective_settings ------------------------------------------------


def test_defaults_when_empty_port_and_no_env():
    """Sans base ni env : renvoie les défauts du module IHM."""
    port = FakeSettingsPort()
    result = uc.get_effective_settings(port)
    assert result["provider"] == "ollama"
    # Défaut du module IHM (historique de app/config/settings.py).
    assert result["model"] == "openrouter/free"
    assert result["timeout_seconds"] == 600


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


def test_validate_training_defaults():
    values = {
        "train_max_per_lang": "800",
        "train_augment_fraction": "0.5",
        "train_variants_per_example": "3",
        "train_use_back_translation": "true",
        "train_epochs": "6",
        "train_batch_size": "16",
        "train_num_workers": "2",
        "train_max_length": "256",
        "train_learning_rate": "0.00003",
        "train_weight_decay": "0.02",
        "train_warmup_ratio": "0.2",
        "train_device": "cuda",
    }
    assert uc.validate_settings(values) == []
    assert values["train_epochs"] == 6
    assert values["train_learning_rate"] == 0.00003
    assert values["train_use_back_translation"] is True


def test_validate_training_defaults_rejects_invalid_device():
    errors = uc.validate_settings({"train_device": "tpu"})
    assert any("train_device" in error for error in errors)


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


# --- Paramètres déplacés de app/config/settings.py (SCRUM-138) ---------------


def test_defaults_include_agent_guards_and_flags():
    """Les réglages déplacés de Settings sont des défauts du module IHM."""
    port = FakeSettingsPort()
    result = uc.get_effective_settings(port)
    assert result["max_llm_rounds"] == 6
    assert result["max_tool_calls"] == 20
    assert result["log_level"] == "INFO"
    assert result["mcp_first"] is False
    assert result["mcp_auth_required"] is True
    assert result["flag_new_core"] is True
    assert result["flag_llm_v2"] is True


def test_env_overrides_agent_guards(monkeypatch):
    """L'env reste un repli de compatibilité pour les réglages déplacés."""
    monkeypatch.setenv("AGENT_MAX_LLM_ROUNDS", "4")
    monkeypatch.setenv("AGENT_MAX_TOOL_CALLS", "9")
    monkeypatch.setenv("AGENT_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("MCP_AUTH_REQUIRED", "0")
    result = uc.get_effective_settings(FakeSettingsPort())
    assert result["max_llm_rounds"] == 4
    assert result["max_tool_calls"] == 9
    assert result["log_level"] == "DEBUG"
    assert result["mcp_auth_required"] is False


def test_persisted_agent_guards_override_env(monkeypatch):
    """La base (MongoDB) reste prioritaire pour les réglages déplacés."""
    monkeypatch.setenv("AGENT_MAX_LLM_ROUNDS", "4")
    port = FakeSettingsPort({"max_llm_rounds": 8, "flag_context": False})
    result = uc.get_effective_settings(port)
    assert result["max_llm_rounds"] == 8
    assert result["flag_context"] is False


def test_validate_rejects_moved_settings_out_of_bounds():
    """Validation métier des réglages déplacés (bornes + énumérations)."""
    errors = uc.validate_settings(
        {
            "max_llm_rounds": 0,
            "max_tool_calls": 500,
            "log_level": "LOUD",
            "flag_new_core": "maybe",
        }
    )
    assert any("max_llm_rounds" in e for e in errors)
    assert any("max_tool_calls" in e for e in errors)
    assert any("log_level" in e for e in errors)
    assert any("flag_new_core" in e for e in errors)


def test_validate_coerces_moved_settings():
    """Les valeurs valides sont coercées (entiers, niveau, booléens)."""
    values = {
        "max_llm_rounds": "4",
        "max_tool_calls": "9",
        "log_level": "debug",
        "mcp_first": "true",
        "flag_context": 0,
    }
    errors = uc.validate_settings(values)
    assert errors == []
    assert values["max_llm_rounds"] == 4
    assert values["max_tool_calls"] == 9
    assert values["log_level"] == "DEBUG"
    assert values["mcp_first"] is True
    assert values["flag_context"] is False


def test_update_persists_moved_settings():
    """Les réglages déplacés sont persistés comme les autres (store IHM)."""
    port = FakeSettingsPort()
    effective, errors, written = uc.update_settings(
        port,
        {
            "max_llm_rounds": 4,
            "max_tool_calls": 9,
            "log_level": "ERROR",
            "mcp_first": True,
            "flag_multi_agent": False,
        },
    )
    assert errors == []
    assert set(written) == {
        "max_llm_rounds",
        "max_tool_calls",
        "log_level",
        "mcp_first",
        "flag_multi_agent",
    }
    assert port._persisted["mcp_first"] is True
    assert effective["flag_multi_agent"] is False
