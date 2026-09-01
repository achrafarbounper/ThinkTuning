# project/api/routes/health.py

import os

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from api.middlewares.maintenance import is_maintenance_mode
from core.job_store import get_job_store
from core.model_sanity import run_model_sanity, VERDICT_OK
from core.model_versioning import list_model_versions, MODEL_ROOT
from core.models import JobStatus
from core.predictor_cache import get_predictor

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


@router.get("/health/model-sanity")
def model_sanity(model: Optional[str] = Query(
    None, alias="model_name",
    description="Version de modèle à vérifier (dossier sous experiments/models). "
                "Par défaut : version active.",
)):
    """Sanity check comportemental du modèle actif (SCRUM-74).

    Exécute Predictor.predict() sur un jeu fixe de phrases FR/EN polarisées
    et détecte un modèle non entraîné ou un fallback base model.
    Retourne 200 avec le rapport si le modèle est sain, 503 avec un verdict
    explicite sinon (« untrained » / « fallback_base_model »).
    """
    try:
        predictor = get_predictor(model)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={"status": "unhealthy", "verdict": "model_unavailable",
                    "detail": f"Impossible de charger le modèle : {exc}"},
        )

    report = run_model_sanity(predictor)
    report["model"] = model
    if report["verdict"] != VERDICT_OK:
        raise HTTPException(
            status_code=503,
            detail={
                "status": report["status"],
                "verdict": report["verdict"],
                "detail": report["detail"],
                "min_confidence": report["min_confidence"],
                "accuracy": report["accuracy"],
                "results": report["results"],
            },
        )
    return report
