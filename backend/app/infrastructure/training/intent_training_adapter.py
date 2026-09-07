# project/app/infrastructure/training/intent_training_adapter.py
"""Adaptateurs legacy du domaine intention (Phase 3d-2 — SCRUM-95).

Enveloppent ``core.intent_trainer`` (thread worker + events d'annulation
DÉDIÉS à l'intention — distincts de ``core.trainer_runner``) et
``core.intent_store`` (versions + pointeur ``active.json``), par attribut
de module (convention du projet : monkeypatchs préservés). Conversions
d'erreurs legacy -> domaine concentrées ICI :

    - ``RuntimeError`` (version source invalide, version inconnue à
      l'activation) -> ``ValidationError`` (422) ;
    - ``RuntimeError`` (job inconnu à l'annulation) -> ``NotFoundError`` (404).
"""

from __future__ import annotations

import os
import threading
import uuid

from app.domain.errors import NotFoundError, ValidationError
from app.domain.ports.training_ports import (
    IntentTrainingRunnerPort,
    IntentVersioningPort,
)
from core import intent_store, intent_trainer
from core.job_store import get_job_store
from core.models import IntentTrainRequest, JobStatus, TrainJob

# Verrou d'écriture du store (réplique le comportement du handler legacy).
_jobs_lock = threading.Lock()


class ModuleIntentTrainingRunnerAdapter:
    """Cycle de vie d'un entraînement d'intention (thread worker dédié)."""

    def precheck(self, request: IntentTrainRequest) -> None:
        """Validations défensives précoces du legacy (échec => 422 avant job)."""
        if not os.path.isfile(request.dataset_path):
            raise ValidationError(f"Dataset introuvable : {request.dataset_path}")
        if request.base_model_version:
            try:
                intent_store.resolve_intent_model_dir(request.base_model_version)
            except RuntimeError as exc:
                raise ValidationError(str(exc)) from exc

    def start(self, request: IntentTrainRequest) -> TrainJob:
        job_id = str(uuid.uuid4())
        job = TrainJob(job_id=job_id, status=JobStatus.PENDING, kind="intent")
        with _jobs_lock:
            get_job_store()[job_id] = job
            # Pré-crée l'Event d'annulation (source unique partagée avec le
            # cancel — même garantie que le handler legacy).
            intent_trainer.get_intent_cancel_event(job_id)
        thread = threading.Thread(
            target=intent_trainer.run_intent_training,
            args=(job_id, request),
            daemon=True,
        )
        thread.start()
        return job

    def cancel(self, job_id: str) -> TrainJob:
        try:
            return intent_trainer.cancel_intent_training(job_id)
        except RuntimeError as exc:
            raise NotFoundError(str(exc) or "job_id introuvable") from exc


class ModuleIntentVersioningAdapter:
    """Versions d'intention + pointeur actif (core.intent_store)."""

    def list_versions(self) -> list[str]:
        return intent_store.list_intent_model_versions()

    def resolve_active_version(self) -> str | None:
        try:
            active_dir = intent_store.resolve_intent_model_dir()
        except RuntimeError:
            return None
        return os.path.basename(os.path.normpath(active_dir))

    def activate(self, version: str) -> None:
        try:
            intent_store.set_active_intent_version(version)
        except RuntimeError as exc:
            raise ValidationError(str(exc)) from exc


def build_default_intent_training_runner() -> IntentTrainingRunnerPort:
    return ModuleIntentTrainingRunnerAdapter()


def build_default_intent_versioning() -> IntentVersioningPort:
    return ModuleIntentVersioningAdapter()
