"""Package des classifieurs (sentiment, intention, ...).

Fournit l'interface commune ``BaseClassifier`` et les implémentations prêtes
à l'emploi. Aucun modèle lourd n'est importé au niveau du package : les
chemins d'inférence (torch/transformers) sont importés paresseusement dans
chaque classifieur, afin que les tests et les environnements sans modèle
restent légers.
"""

from ia.agent.classifiers.base import (
    BaseClassifier,
    ClassifierMetrics,
    PredictionResult,
)
from ia.agent.classifiers.fallback import (
    FallbackClassifier,
    ResilientClassifier,
    fallback_intent,
    fallback_sentiment,
)
from ia.agent.classifiers.intent_classifier import (
    IntentClassifier,
    resolve_intent_model_optional,
)
from ia.agent.classifiers.sentiment_classifier import SentimentClassifier

__all__ = [
    "BaseClassifier",
    "ClassifierMetrics",
    "FallbackClassifier",
    "IntentClassifier",
    "PredictionResult",
    "ResilientClassifier",
    "SentimentClassifier",
    "fallback_intent",
    "fallback_sentiment",
    "resolve_intent_model_optional",
]
