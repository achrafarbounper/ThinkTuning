# project/app/application/models_usecase.py
"""Use-cases du catalogue de modèles et de l'évaluation (Phase 3d-3).

Délégations fines aux ports (parité stricte avec les handlers legacy) : la
logique métier lourde (validation d'artefacts, verdicts de nettoyage,
calcul sklearn) vit côté infrastructure/legacy et reste testable via les
adaptateurs. Les erreurs de domaine (422/404/409/503) sont levées par les
adaptateurs et converties en enveloppe ``{"error": {...}}`` par le handler
global — les routes v1 ne dupliquent aucun try/except.
"""

from __future__ import annotations

from app.domain.ports.model_versioning_ports import (
    EvaluationPort,
    ModelVersioningPort,
)
from core.models import ModelVersion


def list_model_details(*, versioning: ModelVersioningPort) -> list[ModelVersion]:
    """Modèles enregistrés, du plus récent au plus ancien ([] si aucun)."""
    return versioning.list_details()


def get_active_model_pointer(*, versioning: ModelVersioningPort) -> dict:
    """Pointeur de la version active ({"activated": False} si aucune)."""
    return versioning.active_pointer()


def activate_model_version(name: str, *, versioning: ModelVersioningPort) -> dict:
    """Active une version (422 artefacts invalides, 404 inconnue)."""
    return versioning.activate(name)


def delete_model_version(name: str, *, versioning: ModelVersioningPort) -> dict:
    """Supprime une version défaillante (422/404/409 — cf. port)."""
    return versioning.delete(name)


def run_confusion_evaluation(
    *,
    model: str | None,
    limit: int,
    max_mistakes: int,
    evaluation: EvaluationPort,
) -> dict:
    """Matrice de confusion + métriques sur l'échantillon de référence."""
    return evaluation.run_confusion(
        model=model, limit=limit, max_mistakes=max_mistakes
    )
