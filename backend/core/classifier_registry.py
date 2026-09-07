"""Registre centralisé des instances de classifieurs.

Singleton thread-safe : un classifieur par nom (``sentiment``, ``intention``,
...). Le registre ne possède pas les modèles — chaque classifieur encapsule
son moteur d'inférence (ex. ``core.predictor_cache``) — mais garantit qu'une
seule instance sert toutes les requêtes : compteurs d'activité cohérents et
verrouillage d'inférence partagé.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ia.agent.classifiers.base import BaseClassifier


class ClassifierRegistry:
    """Registre de classifieurs par nom (accès protégé par un verrou)."""

    def __init__(self) -> None:
        self._classifiers: dict[str, BaseClassifier] = {}
        self._lock = threading.Lock()

    def register(self, classifier: BaseClassifier, name: str | None = None) -> None:
        """Enregistre une instance sous son ``name`` (ou défaut du classifieur)."""
        key = name or classifier.name
        with self._lock:
            self._classifiers[key] = classifier

    def get(self, name: str) -> BaseClassifier | None:
        """Instance enregistrée pour ``name``, ou None."""
        with self._lock:
            return self._classifiers.get(name)

    def get_or_create(
        self,
        name: str,
        factory: Callable[[], BaseClassifier],
    ) -> BaseClassifier:
        """Instance existante, sinon construction paresseuse via ``factory``."""
        with self._lock:
            classifier = self._classifiers.get(name)
            if classifier is None:
                classifier = factory()
                self._classifiers[name] = classifier
            return classifier

    def remove(self, name: str) -> None:
        """Retire l'instance ``name`` du registre (sans la détruire)."""
        with self._lock:
            self._classifiers.pop(name, None)

    def clear(self) -> None:
        """Vide le registre (utile au hot-reload / aux tests)."""
        with self._lock:
            self._classifiers.clear()

    def names(self) -> list[str]:
        """Noms des classifieurs enregistrés (triés pour des sorties stables)."""
        with self._lock:
            return sorted(self._classifiers)

    def all(self) -> list[BaseClassifier]:
        """Instances enregistrées (ordre d'insertion)."""
        with self._lock:
            return list(self._classifiers.values())


# ============================================================
# Singleton global (même pattern que ia/agent/circuit_breaker.py)
# ============================================================

_registry: ClassifierRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> ClassifierRegistry:
    """Retourne le registre singleton (créé paresseusement)."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = ClassifierRegistry()
    return _registry


def reset_registry() -> None:
    """Réinitialise le singleton (à réserver aux tests)."""
    global _registry
    with _registry_lock:
        _registry = None
