"""Adaptateur : état opérationnel legacy (job_store + maintenance) -> SystemStatusPort.

Consultation seule : le compteur de jobs RUNNING (source ``core.job_store``)
et l'état de maintenance (source ``api.middlewares.maintenance``). Les appels
par attribut de module préservent les monkeypatchs des tests
(``core.job_store.get_job_store``, ``api.middlewares.maintenance.is_maintenance_mode``).
"""

from __future__ import annotations

from api.middlewares import maintenance as _legacy_maintenance
from core import job_store as _legacy_jobs
from core.models import JobStatus


class LegacySystemStatusAdapter:
    """Implémentation de ``SystemStatusPort`` au-dessus des modules legacy."""

    def active_running_jobs(self) -> int:
        store = _legacy_jobs.get_job_store()
        return sum(
            1
            for job in store.values()
            if getattr(job, "status", None) == JobStatus.RUNNING
        )

    def maintenance_mode(self) -> bool:
        return bool(_legacy_maintenance.is_maintenance_mode())


def build_default_system_status() -> LegacySystemStatusAdapter:
    """Source d'état système par défaut de l'application."""
    return LegacySystemStatusAdapter()
