"""Règles métier de repli et classifieur résilient (Phase 3).

Quand un modèle est indisponible (crash, surcharge, échec réseau), l'API doit
continuer de répondre plutôt que de renvoyer 503/500. Ce module fournit :

  - des règles lexicales simples et déterministes pour le sentiment (3
    classes) et l'intention (action/chat) ;
  - ``FallbackClassifier`` : classifieur 100 % règles (aucun modèle chargé) ;
  - ``ResilientClassifier`` : wrapper qui protège tout ``BaseClassifier`` par
    le ``CircuitBreaker`` existant (``ia/agent/circuit_breaker.py``) et bascule
    automatiquement sur le fallback quand le circuit est ouvert.

Les règles sont volontairement grossières : elles ne visent que la continuité
de service, jamais la qualité de prédiction.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

from ia.agent.circuit_breaker import CircuitBreaker
from ia.agent.classifiers.base import BaseClassifier, PredictionResult

logger = logging.getLogger("thinktuning.agent.classifiers.fallback")

SENTIMENT_LABELS = ("positive", "negative", "neutral")
INTENT_LABELS = ("action", "chat")

# Marqueurs lexicaux : les mots courts (<= 5 caractères) sont appariés avec
# des frontières de mot pour éviter les faux positifs de sous-chaîne
# (ex. « top » dans « stop », « donne » dans « données »).
_POSITIVE_WORDS = (
    "excellent", "excellente", "formidable", "fantastique", "merveilleux",
    "génial", "genial", "super", "superbe", "top", "parfait", "parfaite",
    "ador", "adore", "j'adore", "j'aime", "bravo", "impressionnant",
    "recommande", "great", "amazing", "awesome", "perfect", "love",
)
_NEGATIVE_WORDS = (
    "horrible", "terrible", "décev", "decev", "dégueulasse", "degueulasse",
    "mauvais", "mauvaise", "nul", "nulle", "raté", "rate", "frustrant",
    "inutile", "désastre", "desastre", "regret", "rembours", "argh", "gênant",
    "genant", "worst", "bad", "hate", "awful",
)
_ACTION_MARKERS = (
    "appelle", "appeler", "lance", "lancer", "exécute", "execute", "exécuter",
    "utilise", "utiliser", "crée", "creer", "créer", "génère", "generer",
    "générer", "télécharge", "telecharge", "télécharger", "cherche",
    "chercher", "recherche", "rechercher", "trouve", "trouver", "calcule",
    "calculer", "liste", "lister", "affiche", "afficher", "ouvre", "ouvrir",
    "envoie", "envoyer", "supprime", "supprimer", "ajoute", "ajouter",
    "modifie", "modifier", "enregistre", "enregistrer", "prépare", "preparer",
    "donne",
)


def _hits(text: str, markers: tuple[str, ...]) -> int:
    """Nombre de marqueurs trouvés dans ``text`` (minuscules)."""
    count = 0
    for marker in markers:
        if len(marker) <= 5 and marker.isalnum():
            if re.search(rf"\b{re.escape(marker)}\b", text):
                count += 1
        elif marker in text:
            count += 1
    return count


def fallback_sentiment(text: str) -> tuple[str, float]:
    """Repli sentiment : positif/négatif par comptage de mots, sinon neutre."""
    lowered = text.lower()
    positive = _hits(lowered, _POSITIVE_WORDS)
    negative = _hits(lowered, _NEGATIVE_WORDS)
    if positive > negative:
        return "positive", min(0.95, 0.62 + 0.10 * positive)
    if negative > positive:
        return "negative", min(0.95, 0.62 + 0.10 * negative)
    return "neutral", 0.6


def fallback_intent(text: str) -> tuple[str, float]:
    """Repli intention : ``action`` si un marqueur d'action est présent."""
    lowered = text.lower()
    score = _hits(lowered, _ACTION_MARKERS)
    if score:
        return "action", min(0.95, 0.62 + 0.08 * score)
    return "chat", 0.7


class FallbackClassifier(BaseClassifier):
    """Classifieur de repli déterminé uniquement par des règles lexicales.

    Même contrat que les classifieurs modèle (``predict(list[str]) ->
    list[PredictionResult]``), mais sans aucune dépendance lourde : utilisable
    dès que le modèle est indisponible ou que le circuit est ouvert.
    """

    def __init__(self, name: str = "sentiment") -> None:
        if name not in ("sentiment", "intent"):
            raise ValueError(
                f"Pas de règles de repli pour le classifieur {name!r} "
                "(attendu : 'sentiment' ou 'intent')."
            )
        self.name = name
        self._rule = fallback_sentiment if name == "sentiment" else fallback_intent

    @property
    def labels(self) -> tuple[str, ...]:
        return SENTIMENT_LABELS if self.name == "sentiment" else INTENT_LABELS

    def load_model(self) -> None:
        return None  # aucune ressource à charger

    def reload(self) -> None:
        return None

    def predict(self, texts: list[str]) -> list[PredictionResult]:
        results: list[PredictionResult] = []
        for text in texts:
            label, confidence = self._rule(text)
            results.append(
                PredictionResult(text=text, label=label, confidence=confidence)
            )
        return results

    def get_model_info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "engine": "rules",
            "labels": list(self.labels),
        }

    def health_check(self) -> dict[str, Any]:
        label, confidence = self._rule("Ceci est un test de repli.")
        return {"ok": True, "label": label, "confidence": confidence, "engine": "rules"}

    def get_metrics(self) -> dict[str, Any]:
        return {"name": self.name, "engine": "rules"}


class ResilientClassifier(BaseClassifier):
    """Wrapper résilient : circuit breaker + repli automatique sur les règles.

    Comportement :
      - circuit CLOSED : inférence du classifieur interne, succès enregistrés ;
      - échec d'inférence : ``record_failure()`` ; si le circuit n'est pas
        encore ouvert, l'erreur est propagée (l'appelant voit le 500) ;
      - circuit OPEN : repli immédiat sur ``FallbackClassifier`` (aucun appel
        au modèle), ce qui garantit la continuité de service ;
      - HALF_OPEN : un seul appel de test est laissé passer (rétablissement
        automatique sur succès, géré par ``ia/agent/circuit_breaker.py``).
    """

    def __init__(
        self,
        inner: BaseClassifier,
        *,
        fallback: BaseClassifier | None = None,
        breaker: CircuitBreaker | None = None,
        name: str | None = None,
    ) -> None:
        self._inner = inner
        self._fallback = fallback or FallbackClassifier(inner.name)
        self._breaker = breaker or CircuitBreaker(
            failure_threshold=3,
            recovery_timeout=30.0,
        )
        self._fallback_count = 0
        self._fallback_lock = threading.Lock()
        self.name = name or f"resilient-{inner.name}"

    def _serve_fallback(self, texts: list[str]) -> list[PredictionResult]:
        with self._fallback_lock:
            self._fallback_count += len(texts)
        return self._fallback.predict(texts)

    def predict(self, texts: list[str]) -> list[PredictionResult]:
        if not texts:
            return []
        if not self._breaker.can_execute():
            logger.warning(
                "%s : circuit OPEN -> repli règles (%d texte(s))",
                self.name,
                len(texts),
            )
            return self._serve_fallback(texts)
        try:
            results = self._inner.predict(texts)
        except Exception as exc:
            self._breaker.record_failure()
            logger.warning("%s : échec d'inférence enregistré (%s)", self.name, exc)
            if not self._breaker.can_execute():
                logger.warning(
                    "%s : circuit passé OPEN -> repli règles (%d texte(s))",
                    self.name,
                    len(texts),
                )
                return self._serve_fallback(texts)
            raise
        self._breaker.record_success()
        return results

    def load_model(self) -> None:
        self._inner.load_model()

    def reload(self) -> None:
        self._inner.reload()
        self._breaker.reset()

    def health_check(self) -> dict[str, Any]:
        return self._inner.health_check()

    def get_model_info(self) -> dict[str, Any]:
        info = self._inner.get_model_info()
        return {**info, "name": self.name, "circuit": self._breaker.state}

    def get_metrics(self) -> dict[str, Any]:
        inner_metrics = self._inner.get_metrics()
        return {
            **inner_metrics,
            "name": self.name,
            "circuit": self._breaker.metrics,
            "fallback_count": self._fallback_count,
        }
