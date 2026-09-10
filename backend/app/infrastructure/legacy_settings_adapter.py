"""Adaptateur : store de paramètres (core/agent_settings.py) -> port.

Encapsule le store persistant derrière ``AgentSettingsPort`` sans aucune
logique nouvelle. Le store est résolu via
``core.agent_settings.get_settings_store()`` (SQLite ou MongoDB selon
``PERSISTENCE_BACKEND``) — le MÊME backend que la lecture du runtime
(``core/agent_cache.agent_config``) : une sauvegarde du dashboard est
immédiatement effective. Convention legacy conservée :
    - ``get_all()`` renvoie les paires persistées (dict vide si aucune) ;
    - ``save_many()`` fait un upsert transactionnel des clés connues ;
    - la validation métier reste dans le use case (séparation des responsabilités).
"""

from __future__ import annotations

from typing import Any

from app.domain.ports import AgentSettingsPort

try:
    from core.agent_settings import get_settings_store
except ImportError as _exc:  # sécurité : le module legacy est requis (fail-fast)
    raise ImportError(
        "core.agent_settings introuvable : adaptateur de paramètres inutilisable."
    ) from _exc


class LegacySettingsAdapter:
    """Implémentation de ``AgentSettingsPort`` au-dessus du store persistant.

    Le store par défaut est ``core.agent_settings.get_settings_store()``
    (SCRUM-137) : avant ce correctif, l'adaptateur instanciait TOUJOURS le
    store SQLite — en mode ``PERSISTENCE_BACKEND=mongodb`` le dashboard
    écrivait dans SQLite pendant que ``agent_config()`` relisait MongoDB
    (paramètres enregistrés mais jamais appliqués). ``store`` reste
    injectable pour les tests.
    """

    def __init__(self, store: Any | None = None) -> None:
        self._store = store or get_settings_store()

    def get_all(self) -> dict[str, Any]:
        """Charge toutes les paires persistées (dict vide si aucune)."""
        return self._store.get_all()

    def save_many(self, values: dict[str, Any]) -> dict[str, Any]:
        """Upsert transactionnel des clés connues ; renvoie ce qui a été écrit."""
        return self._store.save_many(values)


def build_settings_port() -> AgentSettingsPort:
    """Instance par défaut — même backend que la lecture runtime (SCRUM-137)."""
    return LegacySettingsAdapter()
