# project/api/routes/v1/sessions.py

"""Sessions versionnées (strangler — Phase 3d-4).

Délégation aux handlers legacy ``api.routes.sessions`` (parité par
construction). P0 SEC (F4) : toute la surface est AUTHENTIFIÉE —
X-API-Key OU Bearer JWT ; les lectures (liste, messages) acceptent le
scope read (API_KEY_READ ou jeton read/admin), les écritures exigent
admin. Les 404 légitimes sont
traduits en enveloppe v1 ``{"error": ...}``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies.auth import require_api_key_or_jwt, require_read_api_key_or_jwt
from api.routes import sessions as legacy
from app.infrastructure.legacy_errors import convert_legacy_http_error

router = APIRouter(prefix="/sessions", tags=["Sessions (v1)"])


def _call_guarded(func, *args):
    """Appelle un handler legacy protégé (dernier paramètre ``_`` factice)."""
    try:
        return func(*args, True)
    except HTTPException as exc:
        raise convert_legacy_http_error(exc) from exc


@router.get("")
def list_sessions(limit: int = 100, _: bool = Depends(require_read_api_key_or_jwt)):
    """Liste des conversations (protégé — parité avec le legacy durci)."""
    return _call_guarded(legacy.list_sessions, limit)


@router.post("")
def create_session(body: legacy.SessionCreate, _: bool = Depends(require_api_key_or_jwt)):
    """Crée une session vide (titre dérivé du premier message)."""
    return _call_guarded(legacy.create_session, body)


@router.patch("/{session_id}")
def rename_session(
    session_id: str, body: legacy.SessionRename, _: bool = Depends(require_api_key_or_jwt)
):
    """Renomme une conversation (parité legacy : écriture → X-API-Key)."""
    return _call_guarded(legacy.rename_session, session_id, body)


@router.delete("/{session_id}")
def delete_session(session_id: str, _: bool = Depends(require_api_key_or_jwt)):
    """Supprime une conversation et tous ses messages."""
    return _call_guarded(legacy.delete_session, session_id)


@router.get("/{session_id}/messages")
def list_messages(
    session_id: str, limit: int = 200, _: bool = Depends(require_read_api_key_or_jwt)
):
    """Messages d'une conversation, ordre chronologique (protégé — PII)."""
    return _call_guarded(legacy.list_messages, session_id, limit)
