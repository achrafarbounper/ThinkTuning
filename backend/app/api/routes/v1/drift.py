# project/api/routes/v1/drift.py

"""Détection de dérive versionnée (strangler — Phase 3d-5).

Délégation au handler legacy ``api.routes.drift.drift_route``. L'appel direct
rejoue EXACTEMENT la signature FastAPI legacy (``Request`` + ``UploadFile`` +
``Form``) : le framework parse le multipart, l'appelé fait le reste
(``request.form()``, ``request.json()``, ``request.query_params``) — parité
par construction sur les deux modes (CSV multipart et JSON).

Les HTTPException legacy (threshold invalide, CSV illisible, méthode inconnue)
sont converties en enveloppe v1 ``{"error": ...}``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from api.dependencies.auth import require_api_key_or_jwt
from api.routes import drift as legacy
from app.infrastructure.legacy_errors import convert_legacy_http_error

router = APIRouter(prefix="/drift", tags=["Drift (v1)"])


@router.post("")
async def drift(
    request: Request,
    file_a: UploadFile | None = File(default=None),
    file_b: UploadFile | None = File(default=None),
    text_column: str = Form(default="text"),
    _: bool = Depends(require_api_key_or_jwt),
):
    """Détecte une dérive de distribution entre deux batches (CSV ou JSON)."""
    try:
        return await legacy.drift_route(
            request, file_a, file_b, text_column, True
        )
    except HTTPException as exc:
        raise convert_legacy_http_error(exc) from exc
