# project/core/scheduler.py

"""
Planification d'entraînements récurrents (SCRUM-34).

Singleton ``BackgroundScheduler`` (APScheduler 3.x) qui déclenche des jobs
d'entraînement périodiquement. Les définitions sont persistées dans le SQLite
existant de ``PersistentJobStore`` (table ``scheduled_jobs``) et rechargées au
démarrage de l'API : les planifications survivent aux redémarrages.
"""

import logging
import threading
import time
import uuid

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.job_store import get_job_store

logger = logging.getLogger("scheduler")

_scheduler = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> BackgroundScheduler:
    """Renvoie le scheduler singleton (sans le démarrer)."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = BackgroundScheduler(daemon=True)
    return _scheduler


def _execute_scheduled_training(schedule: dict):
    """Exécute un entraînement à partir d'une définition de planification.

    Réutilise exactement la mécanique de POST /train : création d'un TrainJob
    dans le store + thread daemon sur ``run_training``.
    """
    # Imports locaux pour éviter tout cycle au chargement du module.
    from core.trainer_runner import run_training
    from core.models import TrainJob, JobStatus, TrainRequest

    job_id = str(uuid.uuid4())
    store = get_job_store()
    store[job_id] = TrainJob(job_id=job_id, status=JobStatus.PENDING)

    req = TrainRequest(**schedule["train_request"])
    logger.info(
        "Planification %s déclenchée : lancement du job d'entraînement %s",
        schedule["schedule_id"],
        job_id,
    )
    threading.Thread(target=run_training, args=(job_id, req), daemon=True).start()
    return job_id


def _register_schedule(schedule: dict, replace: bool = True):
    """Enregistre (ou met à jour) le job APScheduler de la définition *schedule*."""
    scheduler = get_scheduler()

    if schedule.get("cron"):
        trigger = CronTrigger.from_crontab(schedule["cron"])
    else:
        trigger = IntervalTrigger(minutes=int(schedule["interval_minutes"]))

    scheduler.add_job(
        _execute_scheduled_training,
        trigger=trigger,
        args=[schedule],
        id=schedule["schedule_id"],
        replace_existing=replace,
    )


def ensure_scheduler_started():
    """Démarre le scheduler et recharge les planifications persistées.

    Idempotent : appelé au démarrage de l'API (api/main.py) et par
    POST /train/schedule, il ne fait rien si le scheduler tourne déjà.
    """
    scheduler = get_scheduler()
    with _scheduler_lock:
        if scheduler.running:
            return scheduler
        scheduler.start()

    # Rechargement des planifications persistées (survit aux redémarrages).
    for schedule in get_job_store().get_schedules():
        try:
            _register_schedule(schedule)
            logger.info("Planification %s rechargée", schedule["schedule_id"])
        except Exception:
            logger.exception(
                "Impossible de recharger la planification %s", schedule["schedule_id"]
            )
    return scheduler


def create_schedule(cron=None, interval_minutes=None, train_request=None,
                    schedule_id=None):
    """Crée une planification récurrente, la persiste et l'enregistre.

    Raises:
        ValueError: si le trigger est invalide (cron malformé, intervalle non
            positif, ou les deux champs fournis / absents à la fois).
    """
    if (cron is None) == (interval_minutes is None):
        raise ValueError(
            "Exactement un des deux champs 'cron' ou 'interval_minutes' est requis."
        )

    if schedule_id is None:
        schedule_id = str(uuid.uuid4())

    schedule = {
        "schedule_id": schedule_id,
        "status": "scheduled",
        "trigger": "cron" if cron else "interval",
        "cron": cron,
        "interval_minutes": int(interval_minutes) if interval_minutes else None,
        "next_run_at": None,
        "created_at": time.time(),
        "train_request": dict(train_request or {}),
    }

    # Valide le trigger en le construisant (ValueError si l'expression est
    # invalide). Démarre le scheduler au besoin (idempotent).
    ensure_scheduler_started()
    _register_schedule(schedule)
    job = get_scheduler().get_job(schedule_id)
    if job and job.next_run_time:
        schedule["next_run_at"] = job.next_run_time.timestamp()

    # Persiste dans le SQLite existant (survie aux redémarrages).
    get_job_store().save_schedule(schedule)
    return schedule


def list_schedules():
    """Renvoie les planifications actives avec leur prochaine exécution."""
    scheduler = get_scheduler()
    items = []
    for schedule in get_job_store().get_schedules():
        if schedule.get("status") == "removed":
            continue
        try:
            if scheduler.running:
                job = scheduler.get_job(schedule["schedule_id"])
                if job is None:
                    # Job disparu du scheduler (persistance mémoire) : re-registre.
                    _register_schedule(schedule)
                    job = scheduler.get_job(schedule["schedule_id"])
                if job and job.next_run_time:
                    schedule["next_run_at"] = job.next_run_time.timestamp()
        except Exception:
            logger.exception(
                "Erreur en rafraîchissant la planification %s",
                schedule["schedule_id"],
            )
        items.append(schedule)
    return items


def delete_schedule(schedule_id: str) -> bool:
    """Supprime une planification (scheduler + persistance)."""
    scheduler = get_scheduler()
    try:
        if scheduler.running:
            scheduler.remove_job(schedule_id)
    except Exception:
        # Le job n'existe pas (ou plus) dans le scheduler : on supprime
        # quand même la définition persistée.
        pass
    return get_job_store().delete_schedule(schedule_id)
