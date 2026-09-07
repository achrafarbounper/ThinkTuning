"""Use-case de prédiction de sentiment — extrait du flux critique de migration.

``run_predict`` et ``run_reload_with_sanity`` encapsulent les règles métier du
flux prédiction (extraction du flux legacy ``api/routes/predict.py``) :

    - la prédiction est déléguée au port (infrastructure) : le use-case ne
      connaît ni Transformers, ni le cache LRU, ni FastAPI ;
    - le rechargement SANS sanity check VALIDE est refusé (SCRUM-74) : un
      modèle non entraîné ne doit jamais servir de prédiction, l'erreur
      ``ModelSanityError`` est mappée en 503 par la couche HTTP.

Les collaborateurs sont INJECTÉS (même convention que ``ask_usecase``) : les
routes ``api/routes/v1/`` résolvent les ports via la composition root
(``api/dependencies/composition.py``) ; les tests injectent des fakes.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.entities.prediction import PredictionResult, SanityReport
from app.domain.errors import ModelSanityError
from app.domain.ports.prediction_ports import PredictionPort


@dataclass(frozen=True)
class PredictCommand:
    """Commande d'entrée du use-case (aucune dépendance framework)."""

    texts: list[str]
    model_name: str | None = None


def run_predict(
    command: PredictCommand, *, predictor: PredictionPort
) -> list[PredictionResult]:
    """Prédit le sentiment des phrases fournies (ordre préservé).

    La validation d'entrée (bornes anti-DoS, phrase non vide) reste dans le
    schéma HTTP (fail-fast 422) ; ici, seule la règle métier s'applique :
    délégation au port, qui lève ``ModelNotAvailableError`` si le modèle
    est indisponible.
    """
    return predictor.predict(command.texts, command.model_name)


def run_reload_with_sanity(*, predictor: PredictionPort) -> SanityReport:
    """Recharge le modèle actif et refuse le rechargement d'un modèle non sain.

    Réplique la décision métier de ``POST /predict/reload`` legacy : le
    rechargement n'est confirmé que si le sanity check conclut ``ok``.
    """
    report = predictor.reload()
    if not report.ok:
        raise ModelSanityError(
            report.detail or f"Modèle non sain après rechargement [{report.verdict}]",
            details={
                "status": "reload_rejected",
                "verdict": report.verdict,
                "min_confidence": report.min_confidence,
                "accuracy": report.accuracy,
            },
        )
    return report
