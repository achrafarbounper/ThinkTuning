# project/app/domain/ports/model_versioning_ports.py
"""Ports du domaine « versions de modèles » et évaluation (Phase 3d-3).

Découplent le dashboard du legacy ``api/routes/models.py`` et
``api/routes/evaluate.py`` :

    - ``ModelVersioningPort`` : consultation du catalogue (détails, pointeur
      actif), activation et nettoyage d'une version défaillante ;
    - ``EvaluationPort`` : matrice de confusion sur un échantillon de
      référence (page « Évaluation » du dashboard).

Les adaptateurs (``app/infrastructure/ml/model_versioning_adapter.py``)
enveloppent les handlers legacy PAR ATTRIBUT DE MODULE et convertissent
leurs ``HTTPException`` en erreurs de domaine (422/404/409/503).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from core.models import ModelVersion


@runtime_checkable
class ModelVersioningPort(Protocol):
    """Catalogue des versions de modèles sentiment (cf. api/routes/models.py)."""

    def list_details(self) -> list[ModelVersion]:
        """Modèles enregistrés, du plus récent au plus ancien (flag ``active``).

        Liste vide si aucun modèle n'est entraîné (parité : 200, pas d'erreur).
        """
        ...

    def active_pointer(self) -> dict:
        """Pointeur de la version active, ou ``{"activated": False}`` si aucune."""
        ...

    def activate(self, name: str) -> dict:
        """Active une version après validation complète de ses artefacts.

        Lève ``ValidationError`` (422 : config/poids/mappings/tête invalides),
        ``NotFoundError`` (404 : version inconnue).
        """
        ...

    def delete(self, name: str) -> dict:
        """Supprime une version DÉFAILLANTE (les versions saines sont refusées).

        Lève ``ValidationError`` (422 : nom invalide ou version saine),
        ``NotFoundError`` (404 : version inconnue), ``ConflictError``
        (409 : version actuellement active).
        """
        ...


@runtime_checkable
class EvaluationPort(Protocol):
    """Évaluation d'un modèle sur un échantillon de référence (confusion)."""

    def run_confusion(
        self, *, model: str | None, limit: int, max_mistakes: int
    ) -> dict:
        """Matrice de confusion + métriques + erreurs par classe + mistakes.

        ``model=None`` => dernière version valide. Lève
        ``ModelNotAvailableError`` (503) si aucun modèle exploitable,
        ``ValidationError`` (422) si l'échantillon de référence est vide.
        """
        ...
