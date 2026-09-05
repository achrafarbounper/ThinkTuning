"""Routes des classifieurs : monitoring + prédiction + rechargement (Phase 5).

Point d'entrée HTTP de la couche ``ia/agent/classifiers`` :
  - ``GET    /classifiers``              → liste + synthèse de santé ;
  - ``GET    /classifiers/{name}``       → instantané (info, métriques, warmup) ;
  - ``POST   /classifiers/{name}/predict`` → prédiction (clé API requise) ;
  - ``POST   /classifiers/{name}/reload``  → rechargement (clé API requise).

Classifieurs connus d'office : ``sentiment`` (DistilBERT par ``predictor_cache``)
et ``intent`` (MiniLM ou repli règles si aucun modèle entraîné). La prédiction
est bornée (mêmes garde-fous anti-DoS que ``api/routes/predict.py``).

Le listing GET est volontairement ouvert (type ``/health``) ; la prédiction et
le rechargement exigent la clé API.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, StringConstraints

from api.dependencies.auth import require_api_key
from core.classifier_monitoring import (
    classifier_snapshot,
    classifier_snapshots,
    health_summary,
)
from core.classifier_registry import get_registry
from ia.agent.classifiers.intent_classifier import IntentClassifier
from ia.agent.classifiers.sentiment_classifier import SentimentClassifier

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Classifiers"])

# Garde-fous d'entrée (mêmes bornes que /predict : env → défaut).
MAX_CLASSIFIER_TEXTS = int(os.getenv("PREDICT_MAX_TEXTS", "256"))
MAX_CLASSIFIER_TEXT_CHARS = int(os.getenv("PREDICT_MAX_TEXT_CHARS", "10000"))

# Fabriques des classifieurs « connus d'office » (créés paresseusement au
# premier accès via le registry singleton — aucun modèle lourd chargé ici).
_DEFAULT_FACTORIES = {
    "sentiment": SentimentClassifier,
    "intent": IntentClassifier,
}


def _resolve_classifier(name: str):
    """Classifieur enregistré, sinon fabrique (sentiment/intent), sinon 404."""
    registry = get_registry()
    existing = registry.get(name)
    if existing is not None:
        return existing
    factory = _DEFAULT_FACTORIES.get(name)
    if factory is None:
        raise HTTPException(status_code=404, detail=f"Classifieur inconnu : {name!r}")
    classifier = registry.get_or_create(name, factory)
    return classifier


class ClassifierPredictRequest(BaseModel):
    texts: list[
        Annotated[str, StringConstraints(max_length=MAX_CLASSIFIER_TEXT_CHARS)]
    ] = Field(min_length=1, max_length=MAX_CLASSIFIER_TEXTS)


class ClassifierPrediction(BaseModel):
    text: str
    label: str
    confidence: float


class ClassifierPredictResponse(BaseModel):
    results: list[ClassifierPrediction]


@router.get("/classifiers")
def list_classifiers():
    """Liste des classifieurs enregistrés + synthèse de santé (monitoring)."""
    snapshots = classifier_snapshots(get_registry())
    return {
        "classifiers": snapshots,
        "summary": health_summary(snapshots),
    }


@router.get("/classifiers/{name}")
def get_classifier(name: str):
    """Instantané d'UN classifieur (info, métriques, warmup, santé)."""
    classifier = _resolve_classifier(name)
    return classifier_snapshot(name, classifier)


@router.post("/classifiers/{name}/predict", response_model=ClassifierPredictResponse)
def predict_classifier(
    name: str,
    req: ClassifierPredictRequest,
    _: bool = Depends(require_api_key),
):
    """Prédiction d'un classifieur sur une liste de textes (ordre préservé)."""
    classifier = _resolve_classifier(name)
    try:
        results = classifier.predict(list(req.texts))
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Prédiction classifieur %s : échec", name)
        raise HTTPException(status_code=500, detail=f"Prédiction impossible : {exc}") from exc
    return {"results": [r.to_dict() for r in results]}


@router.post("/classifiers/{name}/reload")
def reload_classifier(name: str, _: bool = Depends(require_api_key)):
    """Recharge le modèle actif du classifieur depuis le disque."""
    classifier = _resolve_classifier(name)
    try:
        classifier.reload()
    except Exception as exc:
        logger.exception("Rechargement classifieur %s : échec", name)
        raise HTTPException(status_code=500, detail=f"Rechargement impossible : {exc}") from exc
    return {"status": "reloaded", "classifier": name, "model_info": classifier.get_model_info()}
