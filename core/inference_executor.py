"""InferenceExecutor : exécution de l'inférence hors du loop d'événements.

Les routes FastAPI ``async`` ne doivent jamais exécuter de travail CPU lourd
(tokenisation + forward) sur le thread de l'event loop : cela gèlerait TOUTES
les requêtes concurrentes. Cet executor délègue les appels bloquants à un pool
de threads dédié et expose une surcouche ``async`` nativement awaitable.

Singleton global : ``get_executor()`` / ``reset_executor()`` (même pattern que
``ia/agent/circuit_breaker.py`` et ``core/classifier_registry.py``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from functools import partial
from typing import Any, TypeVar

logger = logging.getLogger("thinktuning.core.inference_executor")

_Result = TypeVar("_Result")

_MAX_WORKERS_DEFAULT = min(32, (os.cpu_count() or 1) + 4)


class InferenceExecutor:
    """Pool de threads dédié à l'inférence, avec métriques de charge."""

    def __init__(
        self,
        max_workers: int | None = None,
        thread_name_prefix: str = "inference",
    ) -> None:
        if max_workers is None:
            max_workers = max(2, _MAX_WORKERS_DEFAULT)
        if max_workers < 1:
            raise ValueError("max_workers doit être >= 1")
        self._max_workers = max_workers
        self._pool = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix,
        )
        self._lock = threading.Lock()
        self._submitted = 0
        self._running = 0

    def _tracked(self, fn: Callable[..., _Result], args: tuple, kwargs: dict) -> _Result:
        """Enveloppe une tâche : compte le nombre de tâches réellement en cours."""
        with self._lock:
            self._running += 1
        try:
            return fn(*args, **kwargs)
        finally:
            with self._lock:
                self._running -= 1

    def submit(self, fn: Callable[..., _Result], /, *args: Any, **kwargs: Any) -> Future:
        """Soumet une tâche au pool : retourne un ``Future``."""
        with self._lock:
            self._submitted += 1
        return self._pool.submit(self._tracked, fn, args, kwargs)

    def run(self, fn: Callable[..., _Result], /, *args: Any, **kwargs: Any) -> _Result:
        """Exécute une tâche de façon bloquante (wrapper sur ``submit``)."""
        return self.submit(fn, *args, **kwargs).result()

    async def run_async(self, fn: Callable[..., _Result], /, *args: Any, **kwargs: Any) -> _Result:
        """Exécute ``fn`` dans le pool depuis une coroutine (non-bloquant)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._pool,
            partial(self._tracked, fn, args, kwargs),
        )

    def stats(self) -> dict[str, Any]:
        """Charge du pool : soumissions, tâches en cours, file d'attente."""
        with self._lock:
            return {
                "max_workers": self._max_workers,
                "submitted": self._submitted,
                "running": self._running,
                "queued": max(0, self._submitted - self._running),
            }

    def shutdown(self, wait: bool = True) -> None:
        """Arrête le pool (les tâches en cours terminent si ``wait``)."""
        self._pool.shutdown(wait=wait)


# ============================================================
# Singleton global
# ============================================================

_executor: InferenceExecutor | None = None
_executor_lock = threading.Lock()


def get_executor() -> InferenceExecutor:
    """Retourne l'exécuteur singleton (créé paresseusement)."""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = InferenceExecutor()
    return _executor


def reset_executor() -> None:
    """Arrête et réinitialise le singleton (usage tests / hot-reload)."""
    global _executor
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
            _executor = None
