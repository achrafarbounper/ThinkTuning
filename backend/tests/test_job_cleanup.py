"""
Rétention des jobs : purge des jobs terminés obsolètes (core.job_store.cleanup_old_jobs).

Contrat :
    - seuls les jobs TERMINAUX (completed/failed/cancelled) expirés sont supprimés ;
    - les jobs actifs (pending/running) ne sont jamais touchés, quel que soit l'âge ;
    - dry_run liste sans supprimer ;
    - la purge retire le job de la mémoire, du SQLite et ses métriques d'epochs.
"""
import os
import time

os.environ.setdefault("API_KEY", "test-key")

from core.job_store import PersistentJobStore, cleanup_old_jobs
from core.models import JobStatus, TrainJob


def _make_store(tmp_path):
    return PersistentJobStore(path=str(tmp_path / "jobs.db"))


def _aged(job_id: str, status: JobStatus, age_days: float) -> TrainJob:
    return TrainJob(
        job_id=job_id,
        status=status,
        started_at=time.time() - age_days * 86400,
        finished_at=time.time() - age_days * 86400,
    )


def test_cleanup_deletes_only_expired_terminal_jobs(tmp_path):
    store = _make_store(tmp_path)
    old_completed = _aged("old-completed", JobStatus.COMPLETED, 90)
    old_failed = _aged("old-failed", JobStatus.FAILED, 60)
    fresh_completed = _aged("fresh-completed", JobStatus.COMPLETED, 1)
    old_running = _aged("old-running", JobStatus.RUNNING, 365)  # actif : jamais purgé

    for job in (old_completed, old_failed, fresh_completed, old_running):
        store[job.job_id] = job
        store.update_job_timestamp(job.job_id, time.time() - (job.finished_at and 0 or 0))
    # Vieillit les timestamp DB (c'est ``updated_at`` qui pilote la rétention).
    store.update_job_timestamp("old-completed", time.time() - 90 * 86400)
    store.update_job_timestamp("old-failed", time.time() - 60 * 86400)
    store.update_job_timestamp("old-running", time.time() - 365 * 86400)
    store.update_job_timestamp("fresh-completed", time.time())

    result = store.cleanup_old_jobs(max_age_days=30, dry_run=False)

    assert sorted(result["job_ids"]) == ["old-completed", "old-failed"]
    assert result["deleted"] == 2
    assert store.get("old-completed") is None
    assert store.get("old-failed") is None
    assert store.get("fresh-completed") is not None
    # Un job actif, même très vieux, survit.
    assert store.get("old-running") is not None


def test_cleanup_dry_run_lists_without_deleting(tmp_path):
    store = _make_store(tmp_path)
    store["old-cancelled"] = _aged("old-cancelled", JobStatus.CANCELLED, 120)
    store.update_job_timestamp("old-cancelled", time.time() - 120 * 86400)

    result = store.cleanup_old_jobs(max_age_days=30, dry_run=True)

    assert result["job_ids"] == ["old-cancelled"]
    assert result["deleted"] == 0  # rien supprimé en dry-run
    assert store.get("old-cancelled") is not None


def test_cleanup_module_function_with_custom_db_path(tmp_path):
    """Le contrat du CLI cleanup_old_jobs.py : (max_age_days, dry_run, db_path)."""
    db_path = str(tmp_path / "cli.db")
    store = PersistentJobStore(path=db_path)
    store["ancien"] = _aged("ancien", JobStatus.COMPLETED, 40)
    store.update_job_timestamp("ancien", time.time() - 40 * 86400)

    result = cleanup_old_jobs(max_age_days=30, dry_run=False, db_path=db_path)

    assert result["deleted"] == 1
    assert result["job_ids"] == ["ancien"]
    # La fonction crée une instance dédiée au db_path : la suppression est
    # vérifiée depuis le SQLite (les caches mémoire des autres instances ne
    # sont pas synchronisés — comportement documenté du store).
    verification = PersistentJobStore(path=db_path)
    assert verification.get("ancien") is None


def test_cleanup_purges_epoch_metrics(tmp_path):
    store = _make_store(tmp_path)
    store["job-metrics"] = _aged("job-metrics", JobStatus.COMPLETED, 45)
    store.save_epoch_metrics(
        "job-metrics", [{"epoch": 1, "loss": 0.5, "f1_macro": 0.8, "accuracy": 0.8}]
    )
    store.update_job_timestamp("job-metrics", time.time() - 45 * 86400)
    assert store.get_job_metrics("job-metrics") != []

    store.cleanup_old_jobs(max_age_days=30, dry_run=False)

    assert store.get_job_metrics("job-metrics") == []
