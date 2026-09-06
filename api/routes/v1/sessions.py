# project/api/routes/v1/sessions.py

"""Sessions versionnées (strangler — Phase 3d-4).

Délégation aux handlers legacy ``api.routes.sessions`` (parité par
construction). Posture d'auth IDENTIQUE au legacy : la lecture (liste,
messages) reste publique ; les écritures (création, suppression) exigent
X-API-Key. Les 404 légitimes sont traduits en enveloppe v1 ``{"error": ...}``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies.auth import require_api_key
from api.routes import sessions as legacy
from app.infrastructure.legacy_errors import convert_legacy_http_error

router = APIRouter(prefix="/sessions", tags=["Sessions (v1)"])


def _call_public(func, *args):
    """Appelle un handler legacy PUBLIC (aucun paramètre ``_`` à fournir)."""
    try:
        return func(*args)
    except HTTPException as exc:
        raise convert_legacy_http_error(exc) from exc


def _call_guarded(func, *args):
    """Appelle un handler legacy protégé (dernier paramètre ``_`` factice)."""
    try:
        return func(*args, True)
    except HTTPException as exc:
        raise convert_legacy_http_error(exc) from exc


@router.get("")
def list_sessions(limit: int = 100):
    """Liste des conversations (publique — parité avec le legacy)."""
    return _call_public(legacy.list_sessions, limit)


@router.post("")
def create_session(body: legacy.SessionCreate, _: bool = Depends(require_api_key)):
    """Crée une session vide (titre dérivé du premier message)."""
    return _call_guarded(legacy.create_session, body)


@router.delete("/{session_id}")
def delete_session(session_id: str, _: bool = Depends(require_api_key)):
    """Supprime une conversation et tous ses messages."""
    return _call_guarded(legacy.delete_session, session_id)


@router.get("/{session_id}/messages")
def list_messages(session_id: str, limit: int = 200):
    """Messages d'une conversation, ordre chronologique (public — parité)."""
    return _call_public(legacy.list_messages, session_id, limit)
