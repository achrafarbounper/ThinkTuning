# project/api/routes/sessions.py

"""CRUD des conversations persistées de l'assistant (core/session_store.py).

    GET    /api/sessions                 liste (les plus actives d'abord)
    POST   /api/sessions                 création {title?, model?}
    PATCH  /api/sessions/{id}            renommage {title}
    DELETE /api/sessions/{id}            suppression (messages inclus)
    GET    /api/sessions/{id}/messages   messages chronologiques

Les endpoints d'écriture exigent la clé API (X-API-Key) ; la lecture est
publique comme les autres routes de consultation de l'agent.
"""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from api.dependencies.auth import require_api_key
from core.session_store import get_session_store

router = APIRouter(prefix="/api/sessions", tags=["Sessions"])


class SessionCreate(BaseModel):
    title: Optional[str] = Field(None, max_length=200)
    model: Optional[str] = Field(None, max_length=100)


class SessionRename(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)


@router.get("")
def list_sessions(limit: int = 100):
    """Liste des conversations (id/titre/modèle/horodatages)."""
    return {"sessions": get_session_store().list_sessions(limit=limit)}


@router.post("")
def create_session(body: SessionCreate, _: bool = Depends(require_api_key)):
    """Crée une session vide ; le titre sera dérivé du premier message."""
    return get_session_store().create_session(title=body.title or "", model=body.model or "")


@router.patch("/{session_id}")
def rename_session(session_id: str, body: SessionRename, _: bool = Depends(require_api_key)):
    """Renomme une conversation."""
    renamed = get_session_store().rename_session(session_id, body.title)
    if renamed is None:
        raise HTTPException(status_code=404, detail=f"Session introuvable : {session_id}")
    return renamed


@router.delete("/{session_id}")
def delete_session(session_id: str, _: bool = Depends(require_api_key)):
    """Supprime une conversation et tous ses messages."""
    if not get_session_store().delete_session(session_id):
        raise HTTPException(status_code=404, detail=f"Session introuvable : {session_id}")
    return {"deleted": True, "id": session_id}


@router.get("/{session_id}/messages")
def list_messages(session_id: str, limit: int = 200):
    """Messages d'une conversation, dans l'ordre chronologique.

    Les événements d'outils bruts restent disponibles dans chaque message via
    ``tool_calls`` (mode Agent).
    """
    store = get_session_store()
    if store.get_session(session_id) is None:
        raise HTTPException(status_code=404, detail=f"Session introuvable : {session_id}")
    return {"messages": store.get_messages(session_id, limit=limit)}
