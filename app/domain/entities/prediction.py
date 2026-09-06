"""Value objects de la prédiction de sentiment et de la santé du modèle.

Modèle de domaine PUR : aucune I/O, aucune dépendance FastAPI/legacy. Les
adapters d'infrastructure (app/infrastructure/ml/) construisent ces objets
depuis les structures legacy (dicts retournés par ``Predictor.predict``,
rapports de ``core.model_sanity``) ; les use-cases (app/application/) les
consomment et les routes HTTP (api/routes/v1/) les sérialisent.

Convention ``ok`` : un ``SanityReport`` porte un booléen ``ok`` calculé par
l'adapter en comparant le verdict legacy à ``core.model_sanity.VERDICT_OK``.
Le domaine ne connaît PAS la constante legacy : c'est la frontière
anti-corruption qui garantit qu'un changement de vocabulaire legacy
(ex. nouveau verdict) reste confiné dans l'adapter.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PredictionResult:
    """Résultat de prédiction pour UNE phrase.

    Aligné sur le shape historique de l'API (``text`` / ``sentiment`` /
    ``confidence``) afin que le dashboard consomme v1 sans adaptation ;
    ``model_version`` est un enrichissement v1 (traçabilité de la version
    active au moment de l'inférence).
    """

    text: str
    sentiment: str
    confidence: float
    model_version: str | None = None


@dataclass(frozen=True)
class SanityReport:
    """Issue du sanity check comportemental du modèle (SCRUM-74).

    ``verdict`` est la valeur legacy normalisée (ex. "ok", "untrained",
    "fallback_base_model") ; ``ok`` est le verdict APPLIQUÉ au domaine :
    False => le modèle ne doit PAS servir de prédiction (503 côté API).
    """

    verdict: str
    ok: bool
    status: str = "ok"
    detail: str = ""
    min_confidence: float = 0.0
    accuracy: float = 0.0


@dataclass(frozen=True)
class HealthSnapshot:
    """Photographie instantanée de la santé du service (GET /health).

    Shape identique au ``/health`` legacy (contrat verrouillé par un test
    de non-régression) pour que l'orchestration Docker et le dashboard
    fonctionnent indifféremment sur legacy ou v1.
    """

    status: str
    model_available: bool
    active_jobs: int
    model_dir: str | None
    maintenance_mode: bool
