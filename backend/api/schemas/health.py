# project/api/schemas/health.py


from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Réponse de GET /api/v1/health — shape IDENTIQUE au /health legacy.

    Contrat verrouillé par tests de non-régression : l'orchestration Docker
    et le dashboard consomment indifféremment legacy ou v1.
    """

    status: str
    model_available: bool
    active_jobs: int
    model_dir: str | None = None
    maintenance_mode: bool


class SanityCaseResultOut(BaseModel):
    """Issue du sanity check pour UNE phrase — shape legacy + TS préservés."""

    text: str
    lang: str
    expected: str
    predicted: str
    confidence: float
    correct: bool


class SanityVerdictResponse(BaseModel):
    """Réponse de GET /api/v1/health/model-sanity (modèle sain uniquement).

    Un modèle non sain répond 503 via ``ModelSanityError`` (handler
    DomainError), avec le rapport complet (y compris ``results``) dans
    ``error.details``.
    """

    verdict: str
    status: str
    detail: str
    min_confidence: float
    accuracy: float
    model: str | None = None
    results: list[SanityCaseResultOut] = []


class ReloadResponse(BaseModel):
    """Réponse de POST /api/v1/predict/reload — shape legacy préservé."""

    status: str
    sanity: str
