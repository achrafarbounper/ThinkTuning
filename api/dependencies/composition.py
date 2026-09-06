# project/api/dependencies/composition.py
"""Composition root de l'API — point UNIQUE d'instanciation des dépendances.

Regroupe ce qui était dispersé (fonctions ``get_*`` par route) dans un
container explicite, léger et sans framework externe :

    - ``container.bootstrap()`` enregistre les implémentations par défaut
      (adaptateurs legacy) : appelé au démarrage (fail-fast) depuis
      ``api/main.py`` et paresseusement par les providers ;
    - les routes v1 dépendent des PROVIDERS FastAPI ci-dessous — jamais des
      adaptateurs concrets ;
    - les tests substituent les ports via ``app.dependency_overrides[provider]``
      (pattern officiel FastAPI) ou ``container.register`` pour tout le process.

Strangler : les routes legacy (api/routes/*.py) ne sont PAS modifiées ;
elles continuent d'appeler core.* directement. Les routes v1 passent par
ports + use-cases. La couche legacy sera retirée quand le flux v1 sera
définitivement validé en production.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.domain.ports.prediction_ports import (
    ModelRepositoryPort,
    PredictionPort,
    SystemStatusPort,
)
from app.domain.ports.training_ports import (
    IntentTrainingRunnerPort,
    IntentVersioningPort,
    TrainingJobsPort,
    TrainingRunnerPort,
    TrainingSchedulesPort,
)

# Clés du container (stabilité pour les tests qui voudraient réenregistrer).
KEY_PREDICTION_PORT = "prediction_port"
KEY_MODEL_REPOSITORY = "model_repository_port"
KEY_SYSTEM_STATUS = "system_status_port"
KEY_TRAINING_JOBS = "training_jobs_port"
KEY_TRAINING_RUNNER = "training_runner_port"
KEY_TRAINING_SCHEDULES = "training_schedules_port"
KEY_INTENT_TRAINING_RUNNER = "intent_training_runner_port"
KEY_INTENT_VERSIONING = "intent_versioning_port"


class Container:
    """DI container minimal : factories paresseuses + singletons optionnels.

    Aucune magie d'import : les adaptateurs sont instanciés à la PREMIÈRE
    résolution, donc l'absence de modèle au démarrage (premier lancement
    Docker) ne fait jamais échouer le bootstrap.
    """

    def __init__(self) -> None:
        self._factories: dict[str, Callable[[], Any]] = {}
        self._instances: dict[str, Any] = {}
        self._bootstrapped = False

    def register(
        self, key: str, factory: Callable[[], Any], *, singleton: bool = False
    ) -> None:
        """Enregistre (ou remplace) la factory d'une clé.

        ``singleton=True`` met l'instance en cache après la première
        résolution ; le remplacement d'une clé singleton purge son instance
        (utile aux tests qui réenregistrent des fakes).
        """
        self._factories[key] = factory
        if singleton and key in self._instances:
            del self._instances[key]
        self._bootstrapped = True  # registration manuelle = bootstrappé

    def resolve(self, key: str) -> Any:
        """Résout une clé (instanciation paresseuse, singleton si configuré)."""
        self.ensure_bootstrapped()
        if key in self._instances:
            return self._instances[key]
        factory = self._factories.get(key)
        if factory is None:
            raise KeyError(f"Aucune dépendance enregistrée pour '{key}'")
        instance = factory()
        if key in self._singletons:
            self._instances[key] = instance
        return instance

    @property
    def _singletons(self) -> set[str]:
        return getattr(self, "_singleton_keys", set())

    def ensure_bootstrapped(self) -> None:
        """Enregistre les implémentations par défaut (idempotent)."""
        if self._bootstrapped:
            return
        self.bootstrap()

    def bootstrap(self) -> None:
        """Enregistre les adaptateurs par défaut (adaptateurs legacy)."""
        from app.infrastructure.ml.model_repository_adapter import (
            build_default_repository,
        )
        from app.infrastructure.ml.predictor_adapter import build_default_predictor
        from app.infrastructure.system_status_adapter import build_default_system_status
        from app.infrastructure.training.intent_training_adapter import (
            build_default_intent_training_runner,
            build_default_intent_versioning,
        )
        from app.infrastructure.training.training_adapter import (
            build_default_training_jobs,
            build_default_training_runner,
            build_default_training_schedules,
        )

        self._factories[KEY_PREDICTION_PORT] = build_default_predictor
        self._factories[KEY_MODEL_REPOSITORY] = build_default_repository
        self._factories[KEY_SYSTEM_STATUS] = build_default_system_status
        self._factories[KEY_TRAINING_JOBS] = build_default_training_jobs
        self._factories[KEY_TRAINING_RUNNER] = build_default_training_runner
        self._factories[KEY_TRAINING_SCHEDULES] = build_default_training_schedules
        self._factories[KEY_INTENT_TRAINING_RUNNER] = build_default_intent_training_runner
        self._factories[KEY_INTENT_VERSIONING] = build_default_intent_versioning
        self._singleton_keys: set[str] = set()
        self._bootstrapped = True

    def reset(self) -> None:
        """Vide le container (outillage de tests uniquement)."""
        self._factories.clear()
        self._instances.clear()
        self._singleton_keys = set()
        self._bootstrapped = False


# Container global du process (un seul par worker uvicorn/gunicorn).
container = Container()


# ---------------------------------------------------------------------------
# Providers FastAPI — la SEULE famille de Depends() des routes v1.
# ---------------------------------------------------------------------------
def get_prediction_port() -> PredictionPort:
    """Port d'inférence pour les routes v1 (substituable par override)."""
    return container.resolve(KEY_PREDICTION_PORT)


def get_model_repository_port() -> ModelRepositoryPort:
    """Port de consultation des versions de modèle pour les routes v1."""
    return container.resolve(KEY_MODEL_REPOSITORY)


def get_system_status_port() -> SystemStatusPort:
    """Port d'état opérationnel (jobs, maintenance) pour les routes v1."""
    return container.resolve(KEY_SYSTEM_STATUS)


def get_training_jobs_port() -> TrainingJobsPort:
    """Port de consultation des jobs d'entraînement pour les routes v1."""
    return container.resolve(KEY_TRAINING_JOBS)


def get_training_runner_port() -> TrainingRunnerPort:
    """Port de cycle de vie des entraînements (start/cancel) pour la v1."""
    return container.resolve(KEY_TRAINING_RUNNER)


def get_training_schedules_port() -> TrainingSchedulesPort:
    """Port des planifications récurrentes (SCRUM-34) pour la v1."""
    return container.resolve(KEY_TRAINING_SCHEDULES)


def get_intent_training_runner_port() -> IntentTrainingRunnerPort:
    """Port du cycle de vie des entraînements d'intention pour la v1."""
    return container.resolve(KEY_INTENT_TRAINING_RUNNER)


def get_intent_versioning_port() -> IntentVersioningPort:
    """Port des versions de modèles d'intention (active.json) pour la v1."""
    return container.resolve(KEY_INTENT_VERSIONING)
