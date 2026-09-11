# project/api/routes/v1/models.py
"""Endpoints « modèles sentiment » v1 (Phase 3d-3).

    GET    /api/v1/models/details            catalogue détaillé (flag active)
    GET    /api/v1/models/active             pointeur de la version active
    POST   /api/v1/models/{name}/activate    activation après validation artefacts
    DELETE /api/v1/models/{name}             suppression d'une version défaillante

Parité legacy (``api/routes/models.py``) :
    - details : liste vide (200) si aucun modèle entraîné (pas de 500) ;
    - activate : 422 artefacts invalides, 404 version inconnue ;
    - delete   : 422 (nom invalide OU version saine), 404 inconnue,
      409 version active — enveloppe domaine {error:{code,message,details}} ;
    - NON migrés (non consommés par le dashboard) : GET /models (liste
      simple), GET /models/{name}/report.

Auth : PARITÉ — mêmes ``Depends(require_api_key)`` que le legacy.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from api.dependencies.auth import require_api_key, require_read_api_key
from api.dependencies.composition import get_model_versioning_port
from app.application.models_usecase import (
    activate_model_version,
    delete_model_version,
    get_active_model_pointer,
    list_model_details,
)
from app.domain.ports.model_versioning_ports import ModelVersioningPort
from core.models import ModelVersion

router = APIRouter(tags=["Models v1"])


@router.get("/models/details", response_model=list[ModelVersion])
def list_model_versions_v1(
    _: bool = Depends(require_read_api_key),  # P1 : lecture
    versioning: ModelVersioningPort = Depends(get_model_versioning_port),
) -> list[ModelVersion]:
    """Modèles enregistrés, du plus récent au plus ancien ([] si aucun)."""
    return list_model_details(versioning=versioning)


@router.get("/models/active")
def get_active_model_v1(
    _: bool = Depends(require_read_api_key),  # P1 : lecture
    versioning: ModelVersioningPort = Depends(get_model_versioning_port),
) -> dict:
    """Pointeur de la version active ({"activated": False} si aucune)."""
    return get_active_model_pointer(versioning=versioning)


@router.post("/models/{name}/activate")
def activate_model_v1(
    name: str,
    _: bool = Depends(require_api_key),
    versioning: ModelVersioningPort = Depends(get_model_versioning_port),
) -> dict:
    """Active une version (422 artefacts invalides, 404 inconnue)."""
    return activate_model_version(name, versioning=versioning)


@router.delete("/models/{name}")
def delete_model_v1(
    name: str,
    _: bool = Depends(require_api_key),
    versioning: ModelVersioningPort = Depends(get_model_versioning_port),
) -> dict:
    """Supprime une version défaillante (422 saine, 404 inconnue, 409 active)."""
    return delete_model_version(name, versioning=versioning)
