# project/api/routes/v1/chat.py

"""Chat IA versionné (strangler — Phase 3d-4).

Délégation aux handlers legacy ``api.routes.ai_chat`` : liste des modèles LLM
(``GET /chat/models``, ex-``/models``) et chat streaming SSE (``POST /chat/ai``,
ex-``/ai``). Auth bipolaire X-API-Key OU Bearer JWT : /models en scope
read, POST /ai en action (rôle admin).

Le préfixe ``/chat`` évite toute collision avec le domaine du catalogue de
modèles de sentiment déjà occupé en v1 (``/api/v1/models/*``).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from api.dependencies.auth import require_api_key_or_jwt, require_read_api_key_or_jwt
from api.routes import ai_chat as legacy
from app.domain.errors import GatewayTimeoutError, LLMClientError
from app.infrastructure.legacy_errors import convert_legacy_http_error

router = APIRouter(prefix="/chat", tags=["Chat IA (v1)"])

# 502 provider LLM injoignable / 504 timeout — parité des statuts legacy.
_STATUS_OVERRIDES = {
    502: LLMClientError,
    504: GatewayTimeoutError,
}


def _call_guarded(func, *args):
    """Appelle un handler legacy protégé (dernier paramètre ``_`` factice)."""
    try:
        return func(*args, True)
    except HTTPException as exc:
        raise convert_legacy_http_error(exc, status_overrides=_STATUS_OVERRIDES) from exc


@router.get("/models")
def list_llm_models(_: bool = Depends(require_read_api_key_or_jwt)):
    """Liste les modèles LLM installés sur le serveur (sélecteur du chat)."""
    return _call_guarded(legacy.list_available_llm_models)


@router.post("/ai")
def ai_chat(
    request: legacy.ChatRequest, _: bool = Depends(require_api_key_or_jwt)
) -> StreamingResponse:
    """Chat streaming SSE (thinking_delta / delta / [DONE]) ; erreurs
    pré-stream traduites en 502/504 (enveloppe v1)."""
    return _call_guarded(legacy.ai_chat, request)
