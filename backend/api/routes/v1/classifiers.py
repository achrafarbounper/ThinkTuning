# project/api/routes/v1/classifiers.py

"""Classifieurs versionnés (strangler — Phase 3d-5).

Posture d'auth DECLINÉE comme le legacy :
  - GET /classifiers et GET /classifiers/{name} : PUBLICS (parité /health) ;
  - POST .../predict et POST .../reload : X-API-Key requise.
Les 404 « classifieur inconnu » legacy sont convertis en enveloppe v1.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies.auth import require_api_key
from api.routes import classifiers as legacy
from app.infrastructure.legacy_errors import convert_legacy_http_error

router = APIRouter(prefix="/classifiers", tags=["Classifiers (v1)"])


def _call_public(func, *args):
    try:
        return func(*args)
    except HTTPException as exc:
        raise convert_legacy_http_error(exc) from exc


def _call_guarded(func, *args):
    try:
        return func(*args, True)
    except HTTPException as exc:
        raise convert_legacy_http_error(exc) from exc


@router.get("")
def list_classifiers():
    """Liste des classifieurs + synthèse de santé (public — parité)."""
    return _call_public(legacy.list_classifiers)


@router.get("/{name}")
def get_classifier(name: str):
    """Instantané d'UN classifieur (public — parité)."""
    return _call_public(legacy.get_classifier, name)


@router.post(
    "/{name}/predict",
    response_model=legacy.ClassifierPredictResponse,
    response_model_exclude_none=True,
)
def predict_classifier(
    name: str, req: legacy.ClassifierPredictRequest, _: bool = Depends(require_api_key)
):
    """Prédiction d'un classifieur sur une liste de textes (ordre préservé)."""
    return _call_guarded(legacy.predict_classifier, name, req)


@router.post("/{name}/reload")
def reload_classifier(name: str, _: bool = Depends(require_api_key)):
    """Recharge le modèle actif d'un classifieur depuis le disque."""
    return _call_guarded(legacy.reload_classifier, name)
