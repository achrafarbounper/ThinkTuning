# project/api/routes/v1/metrics.py

"""Métriques versionnées (strangler — Phase 3d-5).

P0 SEC (F4) : les deux endpoints sont authentifiés (l'exposition
Prometheus — noms de routes, volumes — n'est plus publique). Scope
LECTURE : X-API-Key (admin ou API_KEY_READ) OU Bearer JWT (rôle read
ou admin) — le dashboard les consomme via le transport central,
comme /health.
Pas de conversion d'erreur : les handlers ne lèvent pas d'HTTPException
métier (``prometheus_client`` renvoie toujours du contenu).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from api.dependencies.auth import require_read_api_key_or_jwt
from api.routes import metrics as legacy

router = APIRouter(prefix="/metrics", tags=["Metrics (v1)"])


@router.get("")
def metrics(_: bool = Depends(require_read_api_key_or_jwt)) -> Response:
    """Snapshot Prometheus au format texte (lancement du Monitoring)."""
    return legacy.metrics(True)


@router.get("/json")
def metrics_json(_: bool = Depends(require_read_api_key_or_jwt)) -> Response:
    """Repli JSON agrégé (même payload que le legacy)."""
    return legacy.metrics_json(True)
