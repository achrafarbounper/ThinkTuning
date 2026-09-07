"""Adaptateur : versioning de modèles legacy (core.model_versioning) -> ModelRepositoryPort.

Consultation seule (listage, résolution de la version active) : aucune
écriture. Les appels passent par attribut de module, donc les tests qui
redéfinissent ``core.model_versioning.MODEL_ROOT`` (premier lancement Docker
sans modèle, isolation tmp_path) restent effectifs.
"""

from __future__ import annotations

import os

from core import model_versioning as _legacy


class LegacyModelRepositoryAdapter:
    """Implémentation de ``ModelRepositoryPort`` au-dessus du versioning legacy."""

    def list_versions(self) -> list[str]:
        return _legacy.list_model_versions()

    def active_model_dir(self) -> str | None:
        """Réplique la règle legacy de GET /health : version[0] si présente.

        Retourne None (et non une erreur) quand aucune version valide n'existe :
        « pas de modèle » est un état nominal au premier lancement Docker.
        """
        versions = _legacy.list_model_versions()
        if not versions:
            return None
        return os.path.join(_legacy.MODEL_ROOT, versions[0])


def build_default_repository() -> LegacyModelRepositoryAdapter:
    """Dépôt de modèles par défaut de l'application."""
    return LegacyModelRepositoryAdapter()
