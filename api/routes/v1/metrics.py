# project/api/routes/v1/metrics.py

"""Métriques versionnées (strangler — Phase 3d-5).

Les deux endpoints de monitoring restent PUBLICS (parité legacy : aucun
``require_api_key`` — le dashboard les consomme sans clé, comme /health).
Pas de conversion d'erreur : les handlers ne lèvent pas d'HTTPException
métier (``prometheus_client`` renvoie toujours du contenu).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import Response

from api.routes import metrics as legacy

router = APIRouter(prefix="/metrics", tags=["Metrics (v1)"])


@router.get("")
def metrics() -> Response:
    """Snapshot Prometheus au format texte (lancement du Monitoring)."""
    return legacy.metrics()


@router.get("/json")
def metrics_json() -> Response:
    """Repli JSON agrégé (même payload que le legacy)."""
    return legacy.metrics_json()
