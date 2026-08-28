"""Analytique d'utilisation des outils. Phase B — flag AGENT_TOOL_ANALYTICS.

Compteurs in-process (thread-safe) : nombre d'appels, erreurs, durée moyenne
par outil. Les données alimentent ``GET /tools/stats`` et permettent de
repérer les outils inutilisés ou chronophages. Volatil par design (redémarré
avec le process) : la persistance d'usage reste dans run_store.tools_json.
"""

import threading
import time
from collections import defaultdict

_LOCK = threading.Lock()
_STATS: dict[str, dict] = defaultdict(lambda: {"calls": 0, "errors": 0, "total_ms": 0.0})


def reset_usage() -> None:
    """Réinitialise les compteurs (isolation des tests)."""
    with _LOCK:
        _STATS.clear()


def record_usage(tool: str, duration_ms: float, error: bool = False) -> None:
    """Enregistre un appel d'outil (succès ou erreur)."""
    with _LOCK:
        entry = _STATS[tool]
        entry["calls"] += 1
        if error:
            entry["errors"] += 1
        entry["total_ms"] += float(duration_ms)


def record_call(tool: str):
    """Context manager : chronomètre un appel et journalise succès/erreur."""
    class _Timer:
        def __enter__(self):
            self._t0 = time.perf_counter()
            return self

        def __exit__(self, exc_type, _exc, _tb):
            ms = (time.perf_counter() - self._t0) * 1000.0
            record_usage(tool, ms, error=exc_type is not None)
            return False  # ne pas avaler l'exception

    return _Timer()


def get_stats(reset: bool = False) -> dict:
    """Statistiques par outil : calls, errors, error_rate, avg_ms."""
    with _LOCK:
        snapshot = {name: dict(entry) for name, entry in _STATS.items()}
        if reset:
            _STATS.clear()
    out = {}
    for name, e in snapshot.items():
        calls = e["calls"]
        out[name] = {
            "calls": calls,
            "errors": e["errors"],
            "error_rate": round(e["errors"] / calls, 3) if calls else 0.0,
            "avg_ms": round(e["total_ms"] / calls, 1) if calls else 0.0,
        }
    return out
