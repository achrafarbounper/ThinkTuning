"""Adaptateur : état opérationnel legacy (job_store + maintenance) -> SystemStatusPort.

Consultation seule : le compteur de jobs RUNNING (source ``core.job_store``)
et l'état de maintenance (source ``api.middlewares.maintenance``). Les appels
par attribut de module préservent les monkeypatchs des tests
(``core.job_store.get_job_store``, ``api.middlewares.maintenance.is_maintenance_mode``).

IMPORTANT — l'import de ``api.middlewares.maintenance`` est PARESSEUX (dans la
méthode) : un import au niveau module amorce le package ``api`` (façade
d'amorçage), qui peut déclencher ``composition.bootstrap()`` → import de CE
module encore partiellement initialisé → ImportError circulaire. C'est le cas
hors runtime API (transport MCP stdio, tests) : ``app/infrastructure`` ne
doit jamais tirer le package ``api`` au chargement.
"""

from __future__ import annotations

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
        # Import paresseux VOLONTAIRE (voir docstring module) : le package api
        # n'est amorcé qu'au premier appel, jamais au chargement du module.
        from api.middlewares import maintenance as _legacy_maintenance

        return bool(_legacy_maintenance.is_maintenance_mode())


def build_default_system_status() -> LegacySystemStatusAdapter:
    """Source d'état système par défaut de l'application."""
    return LegacySystemStatusAdapter()
