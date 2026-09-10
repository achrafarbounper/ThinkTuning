# project/app/infrastructure/ml/model_versioning_adapter.py
"""Adaptateurs legacy du catalogue de modèles et de l'évaluation (Phase 3d-3).

Enveloppent les handlers de ``api/routes/models.py`` et
``api/routes/evaluate.py`` PAR ATTRIBUT DE MODULE (convention projet : les
monkeypatchs des tests legacy — ``api.routes.models.MODEL_ROOT``,
``api._get_predictor``, ``api.load_raw_dataset``... — restent efficaces
à travers la v1). Seule responsabilité ici : convertir les
``HTTPException`` legacy en erreurs de domaine (le handler global de
``api/errors.py`` fait le reste) :

    422 -> ValidationError | 404 -> NotFoundError | 409 -> ConflictError
    503 -> ModelNotAvailableError
"""

from __future__ import annotations

from fastapi import HTTPException

from api.routes import evaluate as evaluate_module
from api.routes import models as models_module
from app.domain.errors import ModelNotAvailableError
from app.domain.ports.model_versioning_ports import (
    EvaluationPort,
    ModelVersioningPort,
)
from app.infrastructure.legacy_errors import convert_legacy_http_error
from core.models import ModelVersion

# Le 503 legacy de ce domaine signifie « aucun modèle exploitable »
# (même contrat que /predict) — override du mapping commun.
_STATUS_OVERRIDES = {503: ModelNotAvailableError}


class ModuleModelVersioningAdapter:
    """Catalogue des versions de modèles sentiment (handlers legacy)."""

    def list_details(self) -> list[ModelVersion]:
        return models_module.list_models_details()

    def active_pointer(self) -> dict:
        return models_module.get_active_version()

    def activate(self, name: str) -> dict:
        try:
            return models_module.activate_model_version(name)
        except HTTPException as exc:
            raise convert_legacy_http_error(exc, status_overrides=_STATUS_OVERRIDES) from exc

    def delete(self, name: str) -> dict:
        try:
            return models_module.delete_model_version(name)
        except HTTPException as exc:
            raise convert_legacy_http_error(exc, status_overrides=_STATUS_OVERRIDES) from exc


class ModuleEvaluationAdapter:
    """Matrice de confusion sur l'échantillon de référence (handler legacy)."""

    def run_confusion(self, *, model: str | None, limit: int, max_mistakes: int) -> dict:
        try:
            return evaluate_module.confusion_route(
                model=model, limit=limit, max_mistakes=max_mistakes
            )
        except HTTPException as exc:
            raise convert_legacy_http_error(exc, status_overrides=_STATUS_OVERRIDES) from exc


def build_default_model_versioning() -> ModelVersioningPort:
    return ModuleModelVersioningAdapter()


def build_default_evaluation() -> EvaluationPort:
    return ModuleEvaluationAdapter()
