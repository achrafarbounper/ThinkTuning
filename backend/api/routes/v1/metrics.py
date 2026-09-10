# project/api/routes/v1/metrics.py

"""Métriques versionnées (strangler — Phase 3d-5).

P0 SEC (F4) : les deux endpoints exigent X-API-Key (parité legacy durci) —
l'exposition Prometheus (noms de routes, volumes) n'est plus publique. Le
dashboard les consomme avec clé (transport central), comme /health protégé
par la même clé.
Pas de conversion d'erreur : les handlers ne lèvent pas d'HTTPException
métier (``prometheus_client`` renvoie toujours du contenu).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from api.dependencies.auth import require_api_key
from api.routes import metrics as legacy

router = APIRouter(prefix="/metrics", tags=["Metrics (v1)"])


@router.get("")
def metrics(_: bool = Depends(require_api_key)) -> Response:
    """Snapshot Prometheus au format texte (lancement du Monitoring)."""
    return legacy.metrics(True)


@router.get("/json")
def metrics_json(_: bool = Depends(require_api_key)) -> Response:
    """Repli JSON agrégé (même payload que le legacy)."""
    return legacy.metrics_json(True)
