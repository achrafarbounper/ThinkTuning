"""Ports ML — contrats de prédiction, de dépôt de modèles et d'état système.

Ces ports découvrent le FLUX CRITIQUE de la migration (prédiction + santé) :
les use-cases de la couche application en dépendent, l'infrastructure legacy
(``core/predictor_cache.py``, ``core/model_versioning.py``, ``core/job_store.py``,
``api/middlewares/maintenance.py``) les implémente via des adaptateurs
(``app/infrastructure/ml/``, ``app/infrastructure/system_status_adapter.py``).

Alignement : chaque Protocol reprend les signatures réelles du legacy qu'il
encapsule (aucune sémantique nouvelle) — un simple adaptateur suffit, et les
tests existants qui monkeypatchent les modules legacy continuent de passer
(l'adaptateur appelle par attribut de module).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.domain.entities.prediction import PredictionResult, SanityReport


@runtime_checkable
class PredictionPort(Protocol):
    """Contrat d'inférence de sentiment (cf. core/predictor_cache.get_predictor).

    Toutes les méthodes sont synchrones : l'inférence Transformers est
    CPU/GPU-bound ; les routes FastAPI ``def`` l'exécutent dans le threadpool
    (jamais dans l'event loop).

    Lève ``app.domain.errors.ModelNotAvailableError`` si aucune version de
    modèle exploitable n'est disponible (traduction de la HTTPException 503
    legacy par l'adaptateur).
    """

    def predict(
        self, texts: list[str], model_name: str | None = None
    ) -> list[PredictionResult]:
        """Prédit le sentiment d'une liste non vide de phrases (ordre préservé)."""
        ...

    def sanity_check(self, model_name: str | None = None) -> SanityReport:
        """Sanity check comportemental d'une version (défaut : version active)."""
        ...

    def reload(self) -> SanityReport:
        """Recharge la version active depuis le disque puis la valide.

        Le sanity check post-rechargement fait partie du contrat : un
        rechargement qui aboutit à un modèle non entraîné est un échec
        (cf. legacy POST /predict/reload, 503).
        """
        ...


@runtime_checkable
class ModelRepositoryPort(Protocol):
    """Contrat de consultation des versions de modèles (cf. core/model_versioning)."""

    def list_versions(self) -> list[str]:
        """Versions valides disponibles (triées, la plus récente d'abord)."""
        ...

    def active_model_dir(self) -> str | None:
        """Chemin de la version active, ou None si aucune version valide."""
        ...


@runtime_checkable
class SystemStatusPort(Protocol):
    """Contrat d'état opérationnel global (jobs actifs, mode maintenance)."""

    def active_running_jobs(self) -> int:
        """Nombre de jobs d'entraînement actuellement en statut RUNNING."""
        ...

    def maintenance_mode(self) -> bool:
        """True si le mode maintenance est activé (API dégradée volontairement)."""
        ...
