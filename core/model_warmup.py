"""ModelWarmup : préchargement des modèles en arrière-plan.

Objectif : réduire à ≈0 le « cold start » perçu — le premier appel à un
classifieur ne doit pas payer la totalisation du chargement du modèle. Au
démarrage de l'API, chaque classifieur est réchauffé dans un thread daemon :
``load_model()`` (chargement des poids) puis ``health_check()`` (une inférence
de sonde, qui alimente aussi le cache de résultats).

Non bloquant et défensif : aucune exception ne remonte — l'état est exposé via
``status()`` / ``snapshot()`` et consommé par le monitoring.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from ia.agent.classifiers.base import BaseClassifier

logger = logging.getLogger("thinktuning.core.model_warmup")


class ModelWarmup:
    """Registre d'état de réchauffement des classifieurs (thread-safe)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._warmed: dict[str, bool] = {}
        self._reports: dict[str, dict[str, Any]] = {}
        self._threads: dict[str, threading.Thread] = {}

    def warm(self, classifier: BaseClassifier) -> dict[str, Any]:
        """Réchauffe un classifieur de façon SYNCHRONE (chargement + sonde).

        Ne lève jamais : l'échec est porté par le champ ``ok`` du rapport.
        """
        name = classifier.name
        try:
            classifier.load_model()
            probe = classifier.health_check()
            ok = bool(probe.get("ok", False))
            report = {"name": name, "ok": ok, **{k: v for k, v in probe.items() if k != "ok"}}
        except Exception as exc:  # pragma: no cover - chemin défensif
            logger.warning("Warmup %s : échec (%s)", name, exc)
            ok = False
            report = {"name": name, "ok": False, "error": str(exc)}

        with self._lock:
            self._warmed[name] = ok
            self._reports[name] = report
        logger.info(
            "Warmup %s : %s", name, "OK" if ok else "ÉCHEC (modèle indisponible ?)"
        )
        return report

    def warm_in_background(
        self,
        classifier: BaseClassifier,
        on_done: Callable[[dict[str, Any]], None] | None = None,
    ) -> threading.Thread:
        """Lance le réchauffement dans un thread daemon (non bloquant)."""
        name = classifier.name
        with self._lock:
            existing = self._threads.get(name)
            if existing is not None and existing.is_alive():
                return existing  # déjà en cours : idempotent
            thread = threading.Thread(
                target=self._run_and_notify,
                args=(classifier, on_done),
                name=f"model-warmup-{name}",
                daemon=True,
            )
            self._threads[name] = thread
        thread.start()
        return thread

    def _run_and_notify(
        self,
        classifier: BaseClassifier,
        on_done: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        report = self.warm(classifier)
        if on_done is not None:
            try:
                on_done(report)
            except Exception:  # pragma: no cover - callback de monitoring
                logger.exception("Callback de warmup en échec")

    def is_warmed(self, name: str) -> bool:
        """Vrai si un réchauffement réussi a déjà été enregistré pour ``name``."""
        with self._lock:
            return self._warmed.get(name, False)

    def status(self, name: str) -> dict[str, Any] | None:
        """Rapport de réchauffement d'un classifieur (None si jamais tenté)."""
        with self._lock:
            return self._reports.get(name)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """État global du warmup pour le monitoring."""
        with self._lock:
            return {name: report for name, report in self._reports.items()}


# ============================================================
# Singleton global
# ============================================================

_warmup: ModelWarmup | None = None
_warmup_lock = threading.Lock()


def get_warmup() -> ModelWarmup:
    """Retourne l'instance singleton (créée paresseusement)."""
    global _warmup
    if _warmup is None:
        with _warmup_lock:
            if _warmup is None:
                _warmup = ModelWarmup()
    return _warmup


def reset_warmup() -> None:
    """Réinitialise le singleton (usage tests)."""
    global _warmup
    with _warmup_lock:
        _warmup = None
