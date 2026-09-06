# project/app/domain/ports/training_ports.py
"""Ports du domaine training (Phase 3d — noyau /train).

Kernel partagé assumé : les modèles métier du training (``TrainJob``,
``JobStatus``, ``EpochMetric``, ``ScheduledJob``, ...) vivent dans
``core.models`` — ils sont la source de vérité partagée avec le worker
``core.trainer_runner`` (qui écrit le store) et le store SQLite persistant.
Les dupliquer en « entités domaine » créerait un risque de divergence à
chaque évolution du contrat (``kind``, ``regression``, ``progress``...) sans
aucun bénéfice : l'isolation est assurée par les PORTS (interfaces), pas par
la duplication des DTO (Pydantic est déjà la limite conventionnelle du
domaine ici).
"""

from __future__ import annotations

from typing import Protocol

from core.models import IntentTrainRequest, TrainJob, TrainRequest


class TrainingRunnerPort(Protocol):
    """Cycle de vie d'un entraînement (démarrage thread + annulation)."""

    def start(self, request: TrainRequest) -> TrainJob:
        """Crée le job (pending), l'enregistre puis lance le thread worker."""
        ...

    def cancel(self, job_id: str) -> TrainJob:
        """Annule un job actif. Lève ``NotFoundError`` si job inconnu."""
        ...


class TrainingJobsPort(Protocol):
    """Consultation du store de jobs (lecture seule)."""

    def get(self, job_id: str) -> TrainJob | None:
        """Job par identifiant, ``None`` si inconnu."""
        ...

    def list(
        self,
        *,
        status: str | None,
        kind: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[TrainJob], int]:
        """Page de jobs (tri ``started_at DESC``) + total avant pagination.

        ``kind`` filtre le type de job (``"intent"`` pour
        ``/train/intent/jobs``) ; ``None`` = tous les types (parité
        ``/train/jobs`` legacy, qui ne filtre pas).
        """
        ...

    def metrics(self, job_id: str) -> list[dict]:
        """Lignes de métriques par epoch (liste vide si aucune)."""
        ...


class TrainingSchedulesPort(Protocol):
    """Planifications récurrentes d'entraînement (SCRUM-34)."""

    def create(
        self,
        *,
        cron: str | None,
        interval_minutes: int | None,
        train_request: dict,
    ) -> dict:
        """Crée une planification. Lève ``ValueError`` si paramétrage invalide."""
        ...

    def list(self) -> list[dict]:
        """Planifications actives avec leur prochaine exécution."""
        ...

    def delete(self, schedule_id: str) -> bool:
        """Supprime une planification, ``False`` si inconnue."""
        ...


class IntentTrainingRunnerPort(Protocol):
    """Cycle de vie d'un entraînement d'intention (SCRUM-95).

    Le runner d'intention possède ses PROPRES events d'annulation
    (``core.intent_trainer``) et ses validations défensives : distincts du
    runner sentiment (``TrainingRunnerPort``).
    """

    def precheck(self, request: IntentTrainRequest) -> None:
        """Validations défensives précoces (dataset présent, version source
        résoluble). Lève ``ValidationError`` (422) AVANT création du job."""
        ...

    def start(self, request: IntentTrainRequest) -> TrainJob:
        """Crée le job (pending, kind="intent") puis lance le thread worker."""
        ...

    def cancel(self, job_id: str) -> TrainJob:
        """Annule un job actif. Lève ``NotFoundError`` si job inconnu."""
        ...


class IntentVersioningPort(Protocol):
    """Versions de modèles d'intention + pointeur actif (active.json)."""

    def list_versions(self) -> list[str]:
        """Versions valides, triées par nom décroissant (récentes d'abord)."""
        ...

    def resolve_active_version(self) -> str | None:
        """Version active résolue (pointeur, sinon la plus récente valide) ;
        ``None`` si aucun modèle n'existe (repli de règles côté classifieur)."""
        ...

    def activate(self, version: str) -> None:
        """Pointe ``active.json`` sur une version existante.
        Lève ``ValidationError`` (422) si la version est inconnue/invalide."""
        ...
