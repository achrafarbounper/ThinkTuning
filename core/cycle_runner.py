"""Orchestrateur du cycle complet Active Learning (SCRUM-55).

Sequence :
  1. Fusion des annotations manuelles dans un dataset local
     (core.annotation_store.AnnotationStore.merge_annotations) ;
  2. Construction d'un TrainRequest (continual training depuis la version
     active si disponible, corrections locales injectees via
     local_corrections_path) ;
  3. Delegation a core.trainer_runner.run_training (meme job store que /train) ;
  4. Apres COMPLETED : lecture du f1_macro de la nouvelle version et activation
     automatique si elle ameliore (ou du moins ne degrade pas) la version active.

L'orchestrateur est concu pour etre lance dans un thread daemon (meme pattern
que run_training) et reutilise le job store existant pour le suivi.
"""

import logging
import os
import time

from core.annotation_store import get_annotation_store
from core.model_activation import (
    activate_model,
    get_active_model_dir,
    read_version_f1,
)
from core.model_versioning import list_model_versions

logger = logging.getLogger(__name__)

MERGED_DATASET_PATH = os.path.join("data", "annotations_merged.jsonl")


def _version_from_dir(model_dir: str | None) -> str | None:
    if not model_dir:
        return None
    return os.path.basename(os.path.normpath(model_dir))


def resolve_base_version() -> str | None:
    """Version de depart du continual training : active, sinon derniere valide."""
    active_dir = get_active_model_dir()
    version = _version_from_dir(active_dir)
    if version:
        return version
    versions = list_model_versions()
    return versions[0] if versions else None


def run_cycle(job_id: str, train_req, auto_activate: bool = True) -> None:
    """Execute le cycle complet : merge -> train -> activation conditionnelle.

    ``train_req`` : instance de core.models.TrainRequest deja parametree
    (les champs local_corrections_path et base_model_version sont ecrases).
    Bloquant : a lancer dans un thread daemon.
    """
    from core.job_store import get_job_store
    from core.models import JobStatus
    from core.trainer_runner import run_training

    store = get_job_store()
    job = store[job_id]
    job.progress = getattr(job, "progress", None) or {}

    # --- 1. Fusion des annotations -------------------------------------------
    job.progress["cycle"] = {"step": "merging_annotations", "started_at": time.time()}
    store_ = get_annotation_store()
    if store_.count() == 0:
        job.status = JobStatus.FAILED
        job.error = "Cycle interrompu : aucune annotation disponible. Annotez d'abord via /annotate."
        logger.warning(job.error)
        return
    merge_stats = store_.merge_annotations(output_path=MERGED_DATASET_PATH)
    logger.info("Cycle %s : annotations fusionnees -> %s", job_id, merge_stats)
    job.progress["cycle"]["merge"] = merge_stats
    job.progress["cycle"]["step"] = "training"

    # --- 2/3. Re-entrainement (continual depuis la version active) -----------
    base_version = resolve_base_version()
    train_req.local_corrections_path = MERGED_DATASET_PATH
    if base_version:
        train_req.base_model_version = base_version
    job.progress["cycle"]["base_version"] = base_version

    try:
        run_training(job_id, train_req)
    except Exception as exc:
        job.status = JobStatus.FAILED
        job.error = f"Cycle echoue pendant l'entrainement : {exc}"
        raise

    if job.status != JobStatus.COMPLETED:
        job.progress["cycle"]["step"] = "failed"
        return

    new_version = _version_from_dir(getattr(job, "model_path", None))
    new_f1 = read_version_f1(new_version) if new_version else None

    # --- 4. Activation conditionnelle (sans regression) ----------------------
    job.progress["cycle"]["step"] = "activation"
    activation = {
        "new_version": new_version,
        "new_f1_macro": new_f1,
        "previous_version": base_version,
        "previous_f1_macro": read_version_f1(base_version) if base_version else None,
        "activated": False,
    }
    if not auto_activate:
        activation["reason"] = "auto_activate=False"
    elif not new_version:
        activation["reason"] = "nouvelle version introuvable"
    elif new_f1 is None:
        activation["reason"] = "f1_macro indisponible pour la nouvelle version"
    else:
        previous_f1 = activation["previous_f1_macro"]
        if previous_f1 is None or new_f1 >= previous_f1:
            pointer = activate_model(new_version)
            activation["activated"] = True
            activation["active_pointer"] = pointer
            logger.info(
                "Cycle %s : version %s activee (f1=%s, precedent=%s)",
                job_id, new_version, new_f1, previous_f1,
            )
        else:
            activation["reason"] = (
                f"regression f1_macro ({new_f1} < {previous_f1}) : version precedente conservee"
            )
            logger.warning("Cycle %s : %s", job_id, activation["reason"])

    job.progress["cycle"]["activation"] = activation
    job.progress["cycle"]["step"] = "done"
