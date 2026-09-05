"""Interface commune des classifieurs du système de classification.

Aligné sur l'existant du dépôt :

  - ``src/inference/predictor.py`` : contrat ``predict(list[str]) -> list[dict]``
    avec les clés standardisées ``text`` / ``sentiment`` / ``confidence`` ;
  - ``core/predictor_cache.py`` : cache des *instances* de modèles (LRU).

Ce module ne réinvente pas l'inférence : il normalise le contrat entre les
routes API, l'agent et le monitoring. Chaque classifieur concret (sentiment,
intention, ...) encapsule le moteur d'inférence réel derrière ``BaseClassifier``.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class PredictionResult:
    """Résultat de prédiction normalisé pour UN texte.

    Attributs :
        text:          texte original tel que reçu (jamais modifié) ;
        label:         libellé de la classe prédite (ex. ``positive``) ;
        confidence:    confiance [0, 1] de la prédiction ;
        probabilities: distribution complète des classes (optionnel) ;
        model_name:    nom de la version de modèle utilisée ('' = active) ;
        latency_ms:    durée d'inférence (0.0 si servi depuis le cache) ;
        cached:        True si le résultat provient du cache de prédictions.
    """

    text: str
    label: str
    confidence: float
    probabilities: dict[str, float] | None = None
    model_name: str = ""
    latency_ms: float = 0.0
    cached: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Représentation JSON stable (``probabilities`` omis si absent)."""
        data = asdict(self)
        if data.get("probabilities") is None:
            del data["probabilities"]
        return data


@dataclass(slots=True)
class ClassifierMetrics:
    """Compteurs cumulés d'un classifieur (exposés via ``get_metrics()``).

    Thread-safe : chaque mutation est protégée par un verrou interne.
    """

    predictions: int = 0
    cached_hits: int = 0
    cached_misses: int = 0
    errors: int = 0
    total_latency_ms: float = 0.0
    last_latency_ms: float = 0.0
    _lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)

    def record(self, latency_ms: float, cached: bool, error: bool = False) -> None:
        """Enregistre une prédiction. ``cached`` inert en cas d'erreur."""
        with self._lock:
            self.predictions += 1
            if error:
                self.errors += 1
            elif cached:
                self.cached_hits += 1
            else:
                self.cached_misses += 1
            self.total_latency_ms += latency_ms
            self.last_latency_ms = latency_ms

    def to_dict(self) -> dict[str, Any]:
        """Instantané des compteurs (+ moyenne et hit-rate dérivés)."""
        with self._lock:
            avg = self.total_latency_ms / self.predictions if self.predictions else 0.0
            hit_rate = self.cached_hits / self.predictions if self.predictions else 0.0
            return {
                "predictions": self.predictions,
                "cached_hits": self.cached_hits,
                "cached_misses": self.cached_misses,
                "errors": self.errors,
                "total_latency_ms": round(self.total_latency_ms, 3),
                "last_latency_ms": round(self.last_latency_ms, 3),
                "avg_latency_ms": round(avg, 3),
                "cache_hit_rate": round(hit_rate, 4),
            }


class BaseClassifier(ABC):
    """Interface commune de toutes les implémentations de classifieur.

    Chaque classifieur est lié à une tâche (sentiment, intention, ...) et au
    modèle qui la sert. Les opérations lourdes (chargement, inférence) sont
    encapsulées derrière cette interface afin que les routeurs API, l'agent
    et le monitoring dépendent d'un contrat unique au lieu du mêler torch.
    """

    #: Nom stable du classifieur (clé utilisée par ``ClassifierRegistry``).
    name: str = "base"

    @abstractmethod
    def predict(self, texts: list[str]) -> list[PredictionResult]:
        """Prédit la tâche pour une liste de textes (ordre préservé)."""

    @abstractmethod
    def load_model(self) -> None:
        """Force le chargement du modèle (idempotent, utilisé par le warmup)."""

    @abstractmethod
    def reload(self) -> None:
        """Recharge le modèle actif depuis le disque (nouvelle version)."""

    def get_model_info(self) -> dict[str, Any]:
        """Métadonnées du modèle actuellement actif."""
        return {"name": self.name}

    def health_check(self) -> dict[str, Any]:
        """Vérifie que le classifieur répond sur une phrase de sonde.

        Ne lève jamais : l'échec est porté par le champ ``ok`` pour être
        monitoré sans faire tomber l'API.
        """
        return {"ok": True, "name": self.name, "detail": "health_check non implémentée"}

    def get_metrics(self) -> dict[str, Any]:
        """Compteurs cumulés (prédictions, cache hits, erreurs, latence)."""
        return {"name": self.name}
