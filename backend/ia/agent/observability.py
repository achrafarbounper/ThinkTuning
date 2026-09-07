"""Metric avancees pour l'agent.

Extension de tool_analytics.py avec :
    - Persistence des metriques en memoire
    - Agrégation temporelle (fenetres glissantes)
    - Percentiles de latence (p50, p95, p99)
    - Taux d'erreur par outil
"""

import logging
import threading
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

logger = logging.getLogger("thinktuning.agent.observability")


class MetricSample:
    """Echantillon de metrique pour un appel d'outil."""

    __slots__ = ("tool_name", "duration_ms", "success", "timestamp")

    def __init__(
        self,
        tool_name: str,
        duration_ms: float,
        success: bool,
        timestamp: float,
    ) -> None:
        self.tool_name = tool_name
        self.duration_ms = duration_ms
        self.success = success
        self.timestamp = timestamp


class ObservabilityStore:
    """Store thread-safe pour les metriques avec agregation temporelle."""

    def __init__(self, max_samples: int = 10000) -> None:
        self._samples: List[MetricSample] = []
        self._max_samples = max_samples
        self._lock = threading.Lock()

    def record(
        self,
        tool_name: str,
        duration_ms: float,
        success: bool,
    ) -> None:
        """Enregistre un echantillon de metrique."""
        sample = MetricSample(
            tool_name=tool_name,
            duration_ms=duration_ms,
            success=success,
            timestamp=time.time(),
        )
        with self._lock:
            self._samples.append(sample)
            if len(self._samples) > self._max_samples:
                self._samples = self._samples[-self._max_samples // 2 :]

    def _filtered(self, window_seconds: float) -> List[MetricSample]:
        cutoff = time.time() - window_seconds
        with self._lock:
            return [s for s in self._samples if s.timestamp >= cutoff]

    def get_summary(
        self,
        window_seconds: float = 3600.0,
    ) -> Dict[str, Any]:
        """Resume des metriques sur une fenetre temporelle."""
        samples = self._filtered(window_seconds)
        if not samples:
            return {
                "window_seconds": window_seconds,
                "total_calls": 0,
                "total_errors": 0,
                "error_rate": 0.0,
                "tools": {},
            }

        errors = sum(1 for s in samples if not s.success)
        tool_calls: Dict[str, int] = defaultdict(int)
        tool_errors: Dict[str, int] = defaultdict(int)
        for s in samples:
            tool_calls[s.tool_name] += 1
            if not s.success:
                tool_errors[s.tool_name] += 1

        return {
            "window_seconds": window_seconds,
            "total_calls": len(samples),
            "total_errors": errors,
            "error_rate": round(errors / len(samples), 4),
            "tools": {
                name: {
                    "calls": cnt,
                    "errors": tool_errors.get(name, 0),
                }
                for name, cnt in tool_calls.items()
            },
        }

    def get_tool_metrics(
        self,
        tool_name: str,
        window_seconds: float = 3600.0,
    ) -> Dict[str, Any]:
        """Metriques detaillees pour un outil specifique."""
        samples = self._filtered(window_seconds)
        samples = [s for s in samples if s.tool_name == tool_name]
        if not samples:
            return {
                "tool_name": tool_name,
                "total_calls": 0,
                "error_count": 0,
                "avg_duration_ms": 0.0,
                "min_duration_ms": 0.0,
                "max_duration_ms": 0.0,
                "p50_duration_ms": 0.0,
                "p95_duration_ms": 0.0,
                "p99_duration_ms": 0.0,
            }
        durations = sorted([s.duration_ms for s in samples])
        errors = sum(1 for s in samples if not s.success)
        n = len(durations)
        return {
            "tool_name": tool_name,
            "total_calls": n,
            "error_count": errors,
            "avg_duration_ms": round(sum(durations) / n, 2),
            "min_duration_ms": round(durations[0], 2),
            "max_duration_ms": round(durations[-1], 2),
            "p50_duration_ms": round(durations[int(0.5 * (n - 1))], 2),
            "p95_duration_ms": round(durations[int(0.95 * (n - 1))], 2),
            "p99_duration_ms": round(durations[int(0.99 * (n - 1))], 2),
        }

    def get_slow_tools(
        self,
        threshold_ms: float = 1000.0,
        window_seconds: float = 3600.0,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """Retourne les outils les plus lents sur la fenetre."""
        samples = self._filtered(window_seconds)
        tool_durations: Dict[str, List[float]] = defaultdict(list)
        for s in samples:
            tool_durations[s.tool_name].append(s.duration_ms)

        result = []
        for tool, durs in tool_durations.items():
            avg = sum(durs) / len(durs)
            if avg >= threshold_ms or max(durs) >= threshold_ms:
                result.append({
                    "tool": tool,
                    "avg_duration_ms": round(avg, 2),
                    "max_duration_ms": round(max(durs), 2),
                    "call_count": len(durs),
                })

        result.sort(key=lambda x: x["avg_duration_ms"], reverse=True)
        return result[:limit]

    def get_error_rate(
        self,
        tool_name: Optional[str] = None,
        window_seconds: float = 300.0,
    ) -> float:
        """Taux d'erreur sur une fenetre glissante."""
        samples = self._filtered(window_seconds)
        if tool_name:
            samples = [s for s in samples if s.tool_name == tool_name]
        if not samples:
            return 0.0
        errors = sum(1 for s in samples if not s.success)
        return round(errors / len(samples), 4)

    def reset(self) -> None:
        """Reinitialise toutes les metriques."""
        with self._lock:
            self._samples.clear()


# ============================================================
# Singleton global
# ============================================================

_store: Optional[ObservabilityStore] = None
_store_lock = threading.Lock()


def get_observability_store() -> ObservabilityStore:
    """Retourne l'instance singleton du store."""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = ObservabilityStore()
    return _store


def reset_observability_store() -> None:
    """Reinitialise le singleton (utile pour les tests)."""
    global _store
    with _store_lock:
        _store = None


# ============================================================
# Fonctions pratiques (raccourcis sur le singleton)
# ============================================================

def record_metric(
    tool_name: str,
    duration_ms: float,
    success: bool,
) -> None:
    """Enregistre une metrique sur le store global."""
    get_observability_store().record(tool_name, duration_ms, success)


def get_metrics_summary(window_seconds: float = 3600.0) -> Dict[str, Any]:
    """Resume des metriques sur le store global."""
    return get_observability_store().get_summary(window_seconds)


def get_slow_tools(
    threshold_ms: float = 1000.0,
    window_seconds: float = 3600.0,
    limit: int = 10,
) -> List[Dict[str, Any]]:
    """Outils lents sur le store global."""
    return get_observability_store().get_slow_tools(
        threshold_ms, window_seconds, limit
    )


def get_error_rate(
    tool_name: Optional[str] = None,
    window_seconds: float = 300.0,
) -> float:
    """Taux d'erreur sur le store global."""
    return get_observability_store().get_error_rate(tool_name, window_seconds)


def get_tool_metrics(tool_name: str, window_seconds: float = 3600.0) -> Dict[str, Any]:
    """Metriques detaillees pour un outil specifique."""
    return get_observability_store().get_tool_metrics(tool_name, window_seconds)


def clear_metrics() -> None:
    """Supprime toutes les metriques."""
    get_observability_store().reset()


def reset_observability() -> None:
    """Reinitialise le store (pour les tests)."""
    reset_observability_store()
