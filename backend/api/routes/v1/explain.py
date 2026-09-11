# project/api/routes/v1/explain.py

"""Explication LLM versionnée (strangler — Phase 3d-5).

Délégation au handler legacy ``api.routes.explain.explain_route`` (parité par
construction) ; auth identique (X-API-Key requise, comme le legacy).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies.auth import require_read_api_key
from api.routes import explain as legacy
from app.infrastructure.legacy_errors import convert_legacy_http_error

router = APIRouter(prefix="/explain", tags=["Explication (v1)"])


@router.post("", response_model=legacy.ExplainResponse)
def explain(
    req: legacy.ExplainRequest, _: bool = Depends(require_read_api_key)  # P1 : lecture
):
    """Explique en langage naturel la prédiction d'un texte (via le LLM)."""
    try:
        return legacy.explain_route(req, True)
    except HTTPException as exc:
        raise convert_legacy_http_error(exc) from exc
