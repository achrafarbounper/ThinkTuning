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

from core.models import TrainJob, TrainRequest


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
        self, *, status: str | None, limit: int, offset: int
    ) -> tuple[list[TrainJob], int]:
        """Page de jobs (tri ``started_at DESC``) + total avant pagination."""
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
