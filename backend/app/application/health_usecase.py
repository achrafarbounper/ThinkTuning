"""Use-case de santé du service — extrait du flux critique de migration.

``run_health_check`` et ``run_model_sanity_check`` encapsulent la logique de
``api/routes/health.py`` legacy sans dépendre de FastAPI ni des modules
``core.*`` : les collaborateurs sont injectés (ports), les fakes remplacent
l'infrastructure dans les tests.

Le shape de la réponse (``HealthSnapshot``) reste IDENTIQUE au ``/health``
legacy : l'orchestration Docker (supervisord) et le dashboard fonctionnent
sur les deux surfaces pendant la migration (strangler pattern).
"""

from __future__ import annotations

from app.domain.entities.prediction import HealthSnapshot, SanityReport
from app.domain.ports.prediction_ports import (
    ModelRepositoryPort,
    PredictionPort,
    SystemStatusPort,
)


def run_health_check(
    *, repository: ModelRepositoryPort, status: SystemStatusPort
) -> HealthSnapshot:
    """Constitue la photographie de santé du service.

    Règle métier : ``model_available`` est True seulement si une version
    valide existe (poids non vides) — jamais seulement parce qu'un dossier
    ``experiments/models`` existe.
    """
    model_dir = repository.active_model_dir()
    return HealthSnapshot(
        status="ok",
        model_available=model_dir is not None,
        active_jobs=status.active_running_jobs(),
        model_dir=model_dir,
        maintenance_mode=status.maintenance_mode(),
    )


def run_model_sanity_check(model_name: str | None, *, predictor: PredictionPort) -> SanityReport:
    """Exécute le sanity check comportemental d'une version de modèle.

    Retourne le rapport COMPLET : la décision HTTP (200 rapport vs 503
    ``ModelSanityError``) appartient à la couche d'adaptation HTTP, qui
    dispose du booléen ``ok`` calculé par l'infrastructure.
    """
    return predictor.sanity_check(model_name)
