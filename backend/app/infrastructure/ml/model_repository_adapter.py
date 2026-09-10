"""Adaptateur : versioning de modèles legacy (core.model_versioning) -> ModelRepositoryPort.

Consultation seule (listage, résolution de la version active) : aucune
écriture. Les appels passent par attribut de module, donc les tests qui
redéfinissent ``core.model_versioning.MODEL_ROOT`` (premier lancement Docker
sans modèle, isolation tmp_path) restent effectifs.

P0 SEC (F4) : le chemin absolu du serveur (fingerprinting infra) ne fuit
JAMAIS via l'API — seul le NOM de version est exposé (``mask_model_dir``).
"""

from __future__ import annotations

import os

from core import model_versioning as _legacy


def mask_model_dir(model_dir: str | None) -> str | None:
    """Masque un chemin absolu serveur → nom de version seul (anti-fingerprinting).

    ``experiments/models/20260101T000000Z`` (ou absolu) → ``20260101T000000Z`` ;
    ``None`` → ``None``. Utilisé par GET /health (legacy + v1).
    """
    if not model_dir:
        return None
    return os.path.basename(os.path.normpath(model_dir)) or None


class LegacyModelRepositoryAdapter:
    """Implémentation de ``ModelRepositoryPort`` au-dessus du versioning legacy."""

    def list_versions(self) -> list[str]:
        return _legacy.list_model_versions()

    def active_model_dir(self) -> str | None:
        """NOM de la version active (masqué P0), ou None si aucune version valide.

        Retourne None (et non une erreur) quand aucune version valide n'existe :
        « pas de modèle » est un état nominal au premier lancement Docker.
        """
        versions = _legacy.list_model_versions()
        if not versions:
            return None
        return mask_model_dir(os.path.join(_legacy.MODEL_ROOT, versions[0]))


def build_default_repository() -> LegacyModelRepositoryAdapter:
    """Dépôt de modèles par défaut de l'application."""
    return LegacyModelRepositoryAdapter()
