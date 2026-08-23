# project/api/routes/models.py

import os
import json
from typing import List
from fastapi import APIRouter, Depends, HTTPException
import api
from api.dependencies.auth import require_api_key
from core.model_versioning import list_model_versions, resolve_model_dir, MODEL_ROOT
from core.models import ModelVersion

router = APIRouter(prefix="/models", tags=["Models"])

@router.get("", tags=["Models"])
def list_models(_: bool = Depends(require_api_key)):
    root = api.MODEL_ROOT

    items = []
    for name in sorted(os.listdir(root), reverse=True):
        path = os.path.join(root, name)

        if os.path.isdir(path) and os.path.isfile(os.path.join(path, "training_report.json")):
            items.append({
                "name": name,
                "path": os.path.abspath(path)
            })

    return items


@router.get("/details", response_model=List[ModelVersion])
def list_models_details(_: bool = Depends(require_api_key)):
    """Renvoie la liste des modèles enregistrés, du plus récent au plus ancien."""
    active_model_dir = resolve_model_dir()
    active_model_path = os.path.abspath(active_model_dir)

    model_versions = []
    for name in list_model_versions():
        path = os.path.abspath(os.path.join(MODEL_ROOT, name))
        model_versions.append(
            ModelVersion(
                name=name,
                path=path,
                created_at=os.path.getmtime(path),
                active=(path == active_model_path),
            )
        )
    return model_versions


@router.get("/{name}/report")
def get_model_report(name: str, _: bool = Depends(require_api_key)):
    """Retourne le rapport JSON associé à une version de modèle."""
    version_dir = os.path.join(MODEL_ROOT, name)
    report_path = os.path.join(version_dir, "training_report.json")

    if not os.path.isfile(report_path):
        raise HTTPException(
            status_code=404,
            detail=f"Training report not found for model '{name}'."
        )

    with open(report_path, "r", encoding="utf-8") as fh:
        return json.load(fh)
