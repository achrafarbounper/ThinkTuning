"""Adaptateur : cache de prédicteurs legacy (core.predictor_cache) -> PredictionPort.

Premier adaptateur du flux critique ML (prédiction + santé). AUCUNE logique
métier nouvelle : il délègue au legacy, normalise les types (dicts -> value
objects du domaine) et traduit les exceptions legacy (``fastapi.HTTPException``
503, ``RuntimeError`` de résolution) en erreurs de domaine
(``ModelNotAvailableError``).

Frontière anti-corruption : le booléen ``ok`` d'un ``SanityReport`` est
calculé ICI par comparaison avec ``core.model_sanity.VERDICT_OK`` — le
domaine ne connaît pas le vocabulaire legacy des verdicts.

Les imports passent par l'identité de module (``from core import ...``) et
les appels par attribut (``_cache.get_predictor(...)``) : les tests qui
monkeypatchent ``core.predictor_cache.get_predictor`` ou
``core.model_sanity.run_model_sanity`` continuent de fonctionner.
"""

from __future__ import annotations

import logging
import os

from fastapi import HTTPException

from app.domain.entities.prediction import PredictionResult, SanityReport
from app.domain.errors import ModelNotAvailableError
from app.domain.ports.prediction_ports import PredictionPort

# Legacy : imports par identité de PAQUET réel, appels par attribut de MODULE
# (cf. app/infrastructure/legacy_registry.py) — les monkeypatchs des tests sur
# core.predictor_cache.* / core.model_sanity.* restent donc effectifs.
from core import model_sanity as _legacy_sanity
from core import predictor_cache as _legacy_cache

logger = logging.getLogger(__name__)

# Même variable d'environnement que le legacy POST /predict/batch : l'inférence
# est découpée par chunks pour ne pas tokenizer un lot géant en mémoire (OOM).
_CHUNK_SIZE = int(os.getenv("PREDICT_BATCH_CHUNK_SIZE", "128"))


def _to_model_not_available(exc: Exception) -> ModelNotAvailableError:
    """Traduit une exception legacy « modèle indisponible » en erreur de domaine."""
    if isinstance(exc, HTTPException):
        detail = exc.detail if exc.detail else "Aucun modèle disponible"
        return ModelNotAvailableError(str(detail))
    return ModelNotAvailableError(str(exc))


class LegacyPredictorAdapter:
    """Implémentation de ``PredictionPort`` au-dessus du cache de prédicteurs."""

    def predict(
        self, texts: list[str], model_name: str | None = None
    ) -> list[PredictionResult]:
        try:
            predictor = _legacy_cache.get_predictor(model_name)
        except (HTTPException, RuntimeError) as exc:
            raise _to_model_not_available(exc) from exc

        # Chunking mémoire (comportement legacy) : au-delà du chunk, on découpe
        # pour éviter de tokenizer tout le lot d'un coup.
        raw: list[dict] = []
        for start in range(0, len(texts), _CHUNK_SIZE):
            raw.extend(predictor.predict(texts[start : start + _CHUNK_SIZE]))

        model_dir = getattr(predictor, "model_dir", None)
        version = os.path.basename(model_dir) if model_dir else None
        results = []
        for text, prediction in zip(texts, raw, strict=False):
            results.append(
                PredictionResult(
                    text=text,
                    sentiment=str(prediction["sentiment"]),
                    confidence=float(prediction["confidence"]),
                    model_version=version,
                )
            )
        return results

    def sanity_check(self, model_name: str | None = None) -> SanityReport:
        try:
            predictor = _legacy_cache.get_predictor(model_name)
        except (HTTPException, RuntimeError) as exc:
            raise _to_model_not_available(exc) from exc
        return _report_from_legacy(_legacy_sanity.run_model_sanity(predictor))

    def reload(self) -> SanityReport:
        try:
            _legacy_cache.reload_predictor()
            predictor = _legacy_cache.get_predictor(None)
        except (HTTPException, RuntimeError) as exc:
            raise _to_model_not_available(exc) from exc
        return _report_from_legacy(_legacy_sanity.run_model_sanity(predictor))


def _report_from_legacy(report: dict) -> SanityReport:
    """Convertit le dict legacy de ``run_model_sanity`` en value object.

    ``ok`` est la frontière anti-corruption : seule cette fonction compare
    le verdict legacy à ``VERDICT_OK``.
    """
    verdict = str(report.get("verdict", ""))
    return SanityReport(
        verdict=verdict,
        ok=verdict == _legacy_sanity.VERDICT_OK,
        status=str(report.get("status", "ok")),
        detail=str(report.get("detail", "")),
        min_confidence=float(report.get("min_confidence") or 0.0),
        accuracy=float(report.get("accuracy") or 0.0),
    )


def build_default_predictor() -> PredictionPort:
    """Prédicteur par défaut de l'application (cache LRU legacy)."""
    return LegacyPredictorAdapter()
