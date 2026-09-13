"""ClassifierMonitoring : agrégation des métriques des classifieurs (Phase 5).

Unifie en un point unique l'observabilité des classifieurs enregistrés dans le
``ClassifierRegistry`` : métadonnées du modèle, compteurs d'activité
(prédictions, cache hit-rate, erreurs, latence), état de santé et état du
warmup. Le tout est DÉFENSIF : un classifieur hors-service ne fait jamais
échouer le snapshot global (champs ``error`` en place).

Point de contact des routes : ``GET /classifiers`` / ``GET /classifiers/{name}``
(voir ``api/routes/classifiers.py``).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from core.classifier_registry import ClassifierRegistry

logger = logging.getLogger("thinktuning.core.classifier_monitoring")


def _safe(call, *args, **kwargs) -> dict[str, Any]:
    """Exécute ``call`` en capturant TOUTE exception (défensif)."""
    try:
        result = call(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - l'observabilité ne lève pas
        logger.warning("monitoring %r : échec (%s)", getattr(call, "__name__", call), exc)
        return {"error": str(exc)}
    return result if isinstance(result, dict) else {"value": result}


def _warmup_status(name: str) -> dict[str, Any] | None:
    """État du warmup de ``name`` (None si jamais réchauffé / non suivi)."""
    try:
        from core.model_warmup import get_warmup

        return get_warmup().status(name)
    except Exception:  # pragma: no cover - défensif
        return None


def classifier_snapshot(name: str, classifier: Any) -> dict[str, Any]:
    """Instantané complet d'un classifieur (info + metrics + health + warmup)."""
    info = _safe(classifier.get_model_info)
    metrics = _safe(classifier.get_metrics)
    health = _safe(classifier.health_check)
    return {
        "name": name,
        "info": info,
        "metrics": metrics,
        "health": health,
        "warmup": _warmup_status(name),
        "snapshot_at_ms": int(time.time() * 1000),
    }


def classifier_snapshots(registry: ClassifierRegistry) -> list[dict[str, Any]]:
    """Instantanés de TOUS les classifieurs enregistrés (ordre des noms)."""
    return [
        classifier_snapshot(name, registry.get(name))
        for name in registry.names()
        if registry.get(name) is not None
    ]


def health_summary(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    """Synthèse de disponibilité : combien de classifieurs répondent ok."""
    total = len(snapshots)
    ok = 0
    for snapshot in snapshots:
        health = snapshot.get("health") or {}
        if health.get("ok") is True:
            ok += 1
    return {
        "total": total,
        "healthy": ok,
        "unhealthy": total - ok,
        "status": "ok" if ok == total else ("degraded" if ok else "down"),
    }
