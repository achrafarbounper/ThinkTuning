# project/api/routes/health.py

import os

from fastapi import APIRouter

from api.middlewares.maintenance import is_maintenance_mode
from core.job_store import get_job_store
from core.model_versioning import list_model_versions, MODEL_ROOT
from core.models import JobStatus

router = APIRouter(tags=["Health"])


@router.get("/health")
def health():
    """Statut rapide de l'API : modèle dispo ou non, jobs actifs, maintenance."""
    versions = list_model_versions()
    active_model_dir = os.path.join(MODEL_ROOT, versions[0]) if versions else None

    return {
        "status": "ok",
        "model_available": active_model_dir is not None,
        "active_jobs": sum(
            1 for job in get_job_store().values() if job.status == JobStatus.RUNNING
        ),
        "model_dir": active_model_dir,
        "maintenance_mode": is_maintenance_mode(),
    }
