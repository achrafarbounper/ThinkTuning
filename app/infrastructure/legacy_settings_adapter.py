"""Adaptateur : store de paramètres legacy (core/agent_settings.py) -> port.

Encapsule le store SQLite historique derrière ``AgentSettingsPort`` sans
aucune logique nouvelle. Conventions legacy conservées :
    - ``get_all()`` renvoie les paires persistées (dict vide si aucune) ;
    - ``save_many()`` fait un upsert transactionnel des clés connues ;
    - la validation métier reste dans le use case (séparation des responsabilités).
"""

from __future__ import annotations

from typing import Any

from app.domain.ports import AgentSettingsPort

try:
    from core.agent_settings import AgentSettingsStore as _LegacyStore
except ImportError as _exc:  # sécurité : le module legacy est requis (fail-fast)
    raise ImportError(
        "core.agent_settings introuvable : adaptateur de paramètres inutilisable."
    ) from _exc


class LegacySettingsAdapter:
    """Implémentation de ``AgentSettingsPort`` au-dessus du store legacy."""

    def __init__(self, store: _LegacyStore | None = None) -> None:
        self._store = store or _LegacyStore()

    def get_all(self) -> dict[str, Any]:
        """Charge toutes les paires persistées (dict vide si aucune)."""
        return self._store.get_all()

    def save_many(self, values: dict[str, Any]) -> dict[str, Any]:
        """Upsert transactionnel des clés connues ; renvoie ce qui a été écrit."""
        return self._store.save_many(values)


def build_settings_port() -> AgentSettingsPort:
    """Instance par défaut (singleton legacy sous-jacent)."""
    return LegacySettingsAdapter()
