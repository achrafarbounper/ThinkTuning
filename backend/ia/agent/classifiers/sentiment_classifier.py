"""Classifieur de sentiment (DistilBERT FR/EN) encapsulant le prédicteur existant.

Point d'extension du pipeline : cet adaptateur ne réimplémente PAS l'inférence
(``src/inference/predictor.py`` en fait déjà tout le travail : garde-fous,
chunking, ``inference_mode``, device auto). Il ajoute :

  - l'interface commune ``BaseClassifier`` (contrat des routes / de l'agent) ;
  - le cache de RÉSULTATS LRU + TTL par texte (hit-rate attendu 60-80 %) ;
  - des compteurs d'activité (prédictions, hit/miss, latence, erreurs).

Le modèle est chargé paresseusement via ``core.predictor_cache``. Pour les
tests, monkeypatchez ``_load_predictor`` / ``_reload_predictor`` (fonctions
module) : aucun import torch n'est alors nécessaire.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from core.prediction_result_cache import PredictionResultCache
from ia.agent.classifiers.base import BaseClassifier, ClassifierMetrics, PredictionResult

logger = logging.getLogger("thinktuning.agent.classifiers.sentiment")

_SENTIMENT_LABELS = {"positive", "negative", "neutral"}


def _load_predictor(model_name: str | None) -> Any:
    """Prédicteur de la version ``model_name`` (None = version active)."""
    from core import predictor_cache

    return predictor_cache.get_predictor(model_name)


def _reload_predictor(model_name: str | None) -> Any:
    """Recharge la version active depuis le disque puis renvoie le prédicteur."""
    from core import predictor_cache

    predictor_cache.reload_predictor()
    return predictor_cache.get_predictor(model_name)


class SentimentClassifier(BaseClassifier):
    """Classifieur de sentiment aligné sur le prédicteur DistilBERT 3 classes."""

    name = "sentiment"

    def __init__(
        self,
        model_name: str | None = None,
        cache: PredictionResultCache | None = None,
    ) -> None:
        #: None = version active (pointeur ou dernière) ; sinon nom de version.
        self.model_name = model_name
        self._cache = cache or PredictionResultCache(
            maxsize=int(os.getenv("CLASSIFIER_CACHE_MAXSIZE", "8192")),
            ttl_seconds=float(os.getenv("CLASSIFIER_CACHE_TTL", "300")),
        )
        self._metrics = ClassifierMetrics()
        self._predictor: Any | None = None

    # -- Accès modèle --------------------------------------------------------

    def _get_predictor(self) -> Any:
        """Prédicteur sous-jacent (chargé une seule fois)."""
        if self._predictor is None:
            self._predictor = _load_predictor(self.model_name)
        return self._predictor

    def _key(self, text: str) -> str:
        return self._cache.make_key(self.name, text)

    # -- Interface BaseClassifier --------------------------------------------

    def load_model(self) -> None:
        """Force le chargement du modèle (warmup en arrière-plan)."""
        self._get_predictor()
        logger.info(
            "SentimentClassifier : modèle chargé (version=%s)",
            self.model_name or "active",
        )

    def reload(self) -> None:
        """Recharge la version active et invalide les résultats en cache."""
        self._predictor = _reload_predictor(self.model_name)
        self._cache.clear()
        logger.info("SentimentClassifier : modèle rechargé puis cache purgé")

    def predict(self, texts: list[str]) -> list[PredictionResult]:
        """Prédit le sentiment d'une liste de textes (ordre préservé).

        Les textes déjà connus (cache LRU + TTL) ne re-tokenisent pas ; les
        manqués sont prédits en un seul lot pour préserver l'effet batch du
        prédicteur sous-jacent.
        """
        if not texts:
            return []

        results: list[PredictionResult | None] = [None for _ in texts]
        misses: list[tuple[int, str]] = []

        # Passe 1 : cache hits immédiats, collecte des manqués.
        for idx, text in enumerate(texts):
            cached = self._cache.get(self._key(text))
            if cached is not None:
                results[idx] = PredictionResult(
                    text=text,
                    label=cached["label"],
                    confidence=cached["confidence"],
                    probabilities=cached.get("probabilities"),
                    model_name=cached.get("model_name", ""),
                    cached=True,
                )
                self._metrics.record(0.0, cached=True)
            else:
                misses.append((idx, text))

        # Passe 2 : inférence en un lot pour les seuls manqués.
        if misses:
            predictor = self._get_predictor()
            start = time.perf_counter()
            try:
                raw_results = predictor.predict([t for _, t in misses])
                latency_ms = (time.perf_counter() - start) * 1000.0
            except Exception:
                self._metrics.record(0.0, cached=False, error=True)
                logger.exception(
                    "Prédiction sentiment échouée (%d texte(s))", len(misses)
                )
                raise
            per_item_ms = latency_ms / len(misses)
            for (idx, text), row in zip(misses, raw_results, strict=True):
                entry = {
                    "label": row["sentiment"],
                    "confidence": row["confidence"],
                    "probabilities": None,
                    "model_name": self.model_name or "",
                }
                self._cache.set(self._key(text), entry)
                results[idx] = PredictionResult(
                    text=text,
                    label=row["sentiment"],
                    confidence=row["confidence"],
                    model_name=self.model_name or "",
                    latency_ms=per_item_ms,
                )
                self._metrics.record(per_item_ms, cached=False)

        return [result for result in results if result is not None]

    def get_model_info(self) -> dict[str, Any]:
        """Métadonnées du modèle actif (nom de version, device, labels)."""
        predictor = self._get_predictor()
        return {
            "name": self.name,
            "model_name": self.model_name or "active",
            "device": str(getattr(predictor, "device", "unknown") or "unknown"),
            "labels": sorted(_SENTIMENT_LABELS),
        }

    def health_check(self) -> dict[str, Any]:
        """Sonde : prédit une phrase polarisée et vérifie une réponse valide."""
        try:
            start = time.perf_counter()
            result = self.predict(["Ce produit est formidable !"])
            latency_ms = round((time.perf_counter() - start) * 1000.0, 3)
            ok = bool(result) and result[0].label in _SENTIMENT_LABELS
            return {
                "ok": ok,
                "label": result[0].label if result else None,
                "confidence": result[0].confidence if result else None,
                "latency_ms": latency_ms,
            }
        except Exception as exc:  # pragma: no cover - chemin défensif
            logger.warning("health_check sentiment : échec (%s)", exc)
            return {"ok": False, "detail": str(exc)}

    def get_metrics(self) -> dict[str, Any]:
        """Compteurs d'activité du classifieur + statistiques du cache."""
        return {
            "name": self.name,
            **self._metrics.to_dict(),
            "cache": self._cache.stats(),
        }
