"""Budget hiérarchique multi-agents — pool partagé entre les workers.

Problème : chaque worker d'un run superviseur possède SON propre budget de
rounds (`max_worker_rounds`), donc un run à N workers peut consommer jusqu'à
N × budget sans plafond global. ``BudgetPool`` ajoute le plafond manquant :
un quota total d'appels d'outils partagé par tous les workers du run.

Modèle conservateur (déterministe, sans compteur d'usage dans le legacy
``AgentCore``) : chaque worker admis RÉSERVE un quota ferme (au plus
``per_worker_quota``, borné par le reste du pool) qui borne ses rounds LLM —
donc ses appels d'outils. Le quota réservé n'est pas rendu : la borne reste
valide même dans le pire cas. Un worker qui ne peut pas réserver au moins un
slot est refusé AVANT tout appel LLM (``TOKEN_BUDGET_EXCEEDED`` côté run).

Thread-safe : le dispatch peut être parallèle (``ThreadPoolExecutor``).
"""

from __future__ import annotations

import threading
from typing import Any


class BudgetPool:
    """Quota global d'appels d'outils partagé par les workers d'un run.

    - ``total`` : capacité totale du run (appels d'outils tous workers confondus) ;
    - ``reserve(amount)`` : réserve ferme d'au plus ``amount`` slots ; renvoie
      le quota effectivement accordé (0 = pool épuisé, worker à refuser) ;
    - ``release(amount)`` : rend des slots non consommés (réservé à un usage
      futur quand ``AgentCore`` exposera ses compteurs d'usage) ;
    - ``snapshot()`` : état observabel pour l'audit et le contrat de sortie.
    """

    def __init__(self, total: int, label: str = "supervisor_run") -> None:
        if total <= 0:
            raise ValueError("BudgetPool: total doit être > 0")
        self._total = int(total)
        self._label = label
        self._lock = threading.Lock()
        self._available = int(total)
        self._workers_admitted = 0

    @property
    def total(self) -> int:
        return self._total

    def reserve(self, amount: int) -> int:
        """Réserve ferme d'au plus ``amount`` slots (0 refusé si épuisé)."""
        if amount <= 0:
            return 0
        with self._lock:
            granted = min(amount, self._available)
            if granted > 0:
                self._available -= granted
                self._workers_admitted += 1
            return granted

    def release(self, amount: int) -> None:
        """Rend des slots non consommés (borné par la capacité totale)."""
        if amount <= 0:
            return
        with self._lock:
            self._available = min(self._total, self._available + amount)

    def snapshot(self) -> dict[str, Any]:
        """État du pool pour l'audit / le contrat de sortie de l'orchestrateur."""
        with self._lock:
            return {
                "label": self._label,
                "total": self._total,
                "available": self._available,
                "consumed": self._total - self._available,
                "workers_admitted": self._workers_admitted,
            }


def build_budget_pool(
    max_total_tool_calls: int | None,
    per_worker_quota: int,
) -> BudgetPool | None:
    """Factory : pool configuré, ou ``None`` si le plafond global est désactivé
    (comportement historique V1 préservé)."""
    if not max_total_tool_calls or max_total_tool_calls <= 0:
        return None
    return BudgetPool(total=int(max_total_tool_calls), label="supervisor_run")
