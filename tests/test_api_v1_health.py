# project/tests/test_api_v1_health.py
"""Tests de la surface v1 (GET /api/v1/health*, découplage frontend/backend).

Conventions projet : TestClient + clé API de test (conftest racine). Les
tests valident deux exigences de la migration :

    1. CONTRAT : le shape de /api/v1/health est IDENTIQUE à /health legacy
       (orchestration Docker + dashboard consomment indifféremment) ;
    2. ISOLATION : les routes v1 passent par les ports/use-cases, donc la
       substitution d'infrastructure passe par les modules legacy monkeypatchés
       (l'adaptateur appelle par attribut de module) — aucun chargement de
       modèle réel (CLASSIFIER_WARMUP=0, conftest racine).
"""

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from fastapi.testclient import TestClient

import api  # noqa: F401  (initialise MODEL_ROOT/job store avant le routage)
from api import app

client = TestClient(app)


@pytest.fixture
def no_models(monkeypatch, tmp_path):
    """Premier lancement Docker : aucune version de modèle valide.

    ``api.routes.health.get_job_store`` est aussi patché : la route legacy a
    capturé la fonction PAR VALEUR à l'import, tandis que l'adaptateur v1
    l'appelle par attribut de module — les deux surfaces doivent observer le
    même store pour la comparaison de contrat.
    """
    empty_root = tmp_path / "no-models"
    empty_root.mkdir()
    monkeypatch.setattr("core.model_versioning.MODEL_ROOT", str(empty_root))
    monkeypatch.setattr("core.job_store.get_job_store", lambda: {})
    return empty_root


@pytest.fixture
def one_model(monkeypatch, tmp_path):
    """Une version valide (poids non vides) => version active."""
    root = tmp_path / "models"
    version_dir = root / "20260101T000000Z"
    version_dir.mkdir(parents=True)
    (version_dir / "model.pt").write_bytes(b"fake-weights")
    monkeypatch.setattr("core.model_versioning.MODEL_ROOT", str(root))
    monkeypatch.setattr("core.job_store.get_job_store", lambda: {})
    return version_dir


def test_v1_health_without_model(no_models):
    """Aucun modèle : 200 nominal (état premier lancement), jamais un 500."""
    response = client.get("/api/v1/health")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "ok",
        "model_available": False,
        "active_jobs": 0,
        "model_dir": None,
        "maintenance_mode": False,
    }


def test_v1_health_with_model(one_model):
    """Une version valide : model_available=True et chemin de la version."""
    response = client.get("/api/v1/health")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["model_available"] is True
    assert body["model_dir"] == str(one_model)


def test_v1_health_counts_only_running_jobs(monkeypatch):
    """Comptage des jobs actifs : seuls les RUNNING comptent (règle legacy)."""
    from core.models import JobStatus, TrainJob

    store = {
        "job-running": TrainJob(job_id="job-running", status=JobStatus.RUNNING),
        "job-pending": TrainJob(job_id="job-pending", status=JobStatus.PENDING),
        "job-completed": TrainJob(job_id="job-completed", status=JobStatus.COMPLETED),
    }
    monkeypatch.setattr("core.job_store.get_job_store", lambda: store)

    response = client.get("/api/v1/health")

    assert response.status_code == 200, response.text
    assert response.json()["active_jobs"] == 1


def test_v1_health_reflects_maintenance_mode(monkeypatch):
    """Le mode maintenance est visible via v1 (endpoint EXEMPTÉ du middleware).

    Le patch porte sur le flag interne du middleware : route v1 (via l'adaptateur)
    et middleware partagent la même source de vérité — et l'exemption
    ``/api/v1/health`` permet au healthcheck de signaler l'état au lieu d'être
    bloqué en 503.
    """
    monkeypatch.setattr("api.middlewares.maintenance._MAINTENANCE_MODE", True)

    response = client.get("/api/v1/health")

    assert response.status_code == 200, response.text
    assert response.json()["maintenance_mode"] is True


def test_v1_health_exempt_from_middleware_block(monkeypatch):
    """Exemption symétrique : pendant la maintenance, /api/v1/health reste
    répondant (un healthcheck bloqué est un healthcheck inutile), tandis qu'un
    endpoint métier v1 est bien bloqué par le middleware."""
    monkeypatch.setattr("api.middlewares.maintenance._MAINTENANCE_MODE", True)

    assert client.get("/api/v1/health").status_code == 200
    # Un endpoint métier v1 est bloqué pendant la maintenance :
    blocked = client.get("/api/v1/models/details", headers={"X-API-Key": "test-key"})
    assert blocked.status_code == 503


# ---------------------------------------------------------------------------
# Sanity check (GET /api/v1/health/model-sanity) : substitution du port via
# dependency_overrides (pattern officiel FastAPI), fakes déterministes.
# ---------------------------------------------------------------------------

from api.dependencies.composition import get_prediction_port  # noqa: E402


def test_v1_model_sanity_requires_model():
    """Sanity check sans modèle : 503 avec le payload domaine standard."""
    from app.infrastructure.ml import predictor_adapter

    class _Broken:
        @staticmethod
        def sanity_check(model_name=None):
            raise predictor_adapter._to_model_not_available(
                RuntimeError("No valid model versions found")
            )

    app.dependency_overrides[get_prediction_port] = lambda: _Broken()
    try:
        response = client.get("/api/v1/health/model-sanity")
    finally:
        app.dependency_overrides.pop(get_prediction_port, None)

    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "model_not_available"


def test_v1_model_sanity_unhealthy_model():
    """Modèle présent mais non entraîné : 503 model_unhealthy + rapport complet.

    Parité legacy : ``error.details`` porte le détail PAR PHRASE (``results``),
    que le dashboard affiche dans la vue « Détails » d'un modèle défaillant.
    """
    from app.domain.entities.prediction import SanityCaseResult, SanityReport

    class _Untrained:
        @staticmethod
        def sanity_check(model_name=None):
            return SanityReport(
                verdict="untrained",
                ok=False,
                status="unhealthy",
                detail="Confiance quasi-uniforme sur toutes les classes",
                min_confidence=0.4,
                accuracy=0.25,
                results=(
                    SanityCaseResult(
                        text="Phrase négative",
                        lang="fr",
                        expected="negative",
                        predicted="neutral",
                        confidence=0.31,
                        correct=False,
                    ),
                ),
            )

    app.dependency_overrides[get_prediction_port] = lambda: _Untrained()
    try:
        response = client.get("/api/v1/health/model-sanity")
    finally:
        app.dependency_overrides.pop(get_prediction_port, None)

    assert response.status_code == 503, response.text
    error = response.json()["error"]
    assert error["code"] == "model_unhealthy"
    assert error["details"]["verdict"] == "untrained"
    assert error["details"]["accuracy"] == 0.25
    assert error["details"]["results"] == [
        {
            "text": "Phrase négative",
            "lang": "fr",
            "expected": "negative",
            "predicted": "neutral",
            "confidence": 0.31,
            "correct": False,
        }
    ]


def test_v1_model_sanity_healthy_model():
    """Modèle sain : 200 + rapport (contract shape de SanityVerdictResponse)."""
    from app.domain.entities.prediction import SanityCaseResult, SanityReport

    class _Healthy:
        @staticmethod
        def sanity_check(model_name=None):
            return SanityReport(
                verdict="ok",
                ok=True,
                status="ok",
                detail="8/8 phrases correctement classées",
                min_confidence=0.4,
                accuracy=1.0,
                results=(
                    SanityCaseResult(
                        text="Phrase positive",
                        lang="fr",
                        expected="positive",
                        predicted="positive",
                        confidence=0.93,
                        correct=True,
                    ),
                ),
            )

    app.dependency_overrides[get_prediction_port] = lambda: _Healthy()
    try:
        response = client.get("/api/v1/health/model-sanity?model_name=v-test")
    finally:
        app.dependency_overrides.pop(get_prediction_port, None)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verdict"] == "ok"
    assert body["model"] == "v-test"  # champ de RÉPONSE aligné legacy
    assert body["results"][0]["correct"] is True
