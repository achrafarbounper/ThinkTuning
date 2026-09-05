# project/api/main.py

import os

# --- Configuration logging ----------------------------------------------------
# Sans cette configuration, les loggers de l'agent (`ia.agent.*`, cf. paquet
# ia/) n'ont AUCUN handler : Python n'affiche alors que les WARNING+ sur stderr
# via son handler « last resort », et uvicorn ne configure que ses propres
# loggers (`uvicorn`, `uvicorn.error`, `uvicorn.access`) — jamais ceux de votre
# app.
#
# On branche ici un handler CONSOLE COLORÉ (rich, cf. ia/logging_setup.py) sur
# la racine : tous les logs de l'API ET de l'agent s'affichent lisiblement dans
# le terminal (niveaux en couleur, durées, tracebacks riches). Idempotent :
# aucun doublon même si uvicorn recharge le module. Niveau réglable via la
# variable d'environnement AGENT_LOG_LEVEL (DEBUG/INFO/...).
from ia.logging_setup import setup_agent_logging  # noqa: E402

setup_agent_logging(os.getenv("AGENT_LOG_LEVEL", "INFO"))

from contextlib import asynccontextmanager
import threading

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

# SCRUM-74 : sanity check comportemental du modèle au démarrage de l'API.
# Exécute Predictor.predict() sur un jeu fixe de phrases FR/EN polarisées
# afin de détecter un modèle non entraîné / un fallback base model. L'API
# reste démarrée (disponibilité de /health/model-sanity et de l'outillage),
# mais l'état est loggé en ERROR et exposé via l'endpoint de santé.
def _run_startup_model_sanity() -> None:
    import logging

    from core.model_sanity import run_model_sanity, VERDICT_OK
    from core.predictor_cache import get_predictor

    _logger = logging.getLogger(__name__)
    try:
        predictor = get_predictor()
        report = run_model_sanity(predictor)
        if report["verdict"] == VERDICT_OK:
            _logger.info("Sanity check modèle au démarrage : %s", report["detail"])
        else:
            _logger.error(
                "Sanity check modèle au démarrage ÉCHOUÉ [%s] : %s",
                report["verdict"],
                report["detail"],
            )
    except Exception as exc:
        # Aucun modèle disponible au démarrage (ex. premier lancement Docker)
        # ou échec du check : non bloquant, l'état reste visible via
        # GET /health/model-sanity.
        _logger.warning(
            "Sanity check modèle au démarrage indisponible : %s", exc
        )


def _run_startup_classifier_warmup() -> None:
    """Réchauffe le classifieur de sentiment en arrière-plan (Phase 2).

    Idempotent et défensif : le warmup ne doit JAMAIS faire échouer le
    démarrage. ``ModelWarmup.warm()`` capture toutes les exceptions (y compris
    l'absence de modèle) et expose l'état via ``status()`` / ``snapshot()``.
    """
    import logging

    _logger = logging.getLogger(__name__)
    try:
        from core.classifier_registry import get_registry
        from core.model_warmup import get_warmup
        from ia.agent.classifiers.sentiment_classifier import SentimentClassifier

        classifier = get_registry().get_or_create(
            SentimentClassifier.name, SentimentClassifier
        )
        get_warmup().warm_in_background(classifier)
    except Exception as exc:  # pragma: no cover - défensif
        _logger.warning("Warmup classifieur au démarrage impossible : %s", exc)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Cycle de vie de l'application (remplace @app.on_event, déprécié)."""
    from api.dependencies.auth import warn_if_insecure_api_key

    warn_if_insecure_api_key()
    # Le sanity check charge potentiellement le modèle (plusieurs secondes) :
    # exécuté en thread daemon pour ne pas retarder la disponibilité de l'API.
    # L'état reste visible via GET /health/model-sanity.
    threading.Thread(
        target=_run_startup_model_sanity,
        name="startup-model-sanity",
        daemon=True,
    ).start()
    # Phase 2 : réchauffe le classifieur de sentiment en arrière-plan (le cold
    # start est porté par un thread daemon pendant que l'API répond déjà).
    # Désactivable via CLASSIFIER_WARMUP=0 (c'est le défaut des tests/CI : ne
    # pas charger le modèle de 541 Mo à chaque TestClient).
    if os.getenv("CLASSIFIER_WARMUP", "1") != "0":
        threading.Thread(
            target=_run_startup_classifier_warmup,
            name="startup-classifier-warmup",
            daemon=True,
        ).start()
    yield


from api.routes import (  # noqa: E402
    active_learning,
    agent,
    ai_chat,
    classifiers,
    drift,
    evaluate,
    explain,
    health,
    maintenance,
    metrics,
    models,
    pipeline,
    predict,
    sessions,
    train,
)
from core.scheduler import ensure_scheduler_started  # noqa: E402
from api.middlewares.maintenance import maintenance_mode_middleware  # noqa: E402
from api.middlewares.rate_limit import rate_limit_middleware  # noqa: E402
from api.middlewares.metrics import request_metrics_middleware  # noqa: E402


def _cors_allowed_origins() -> list[str]:
    """Origines CORS : variable d'environnement (CSV), sinon defaults locaux.

    docker-compose injecte déjà CORS_ALLOWED_ORIGINS ; le hardcode historique
    empêchait tout déploiement hors localhost sans modifier le code.
    """
    raw = os.getenv("CORS_ALLOWED_ORIGINS", "")
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return origins or [
        "http://localhost",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


app = FastAPI(
    title="Sentiment Analysis API",
    description="Entraînement et prédiction pour l'analyse de sentiments FR/EN",
    version="1.0.0",
    lifespan=lifespan,
)

# Ordonancement explicite — en Starlette, le DERNIER middleware ajouté est le
# plus EXTERNE. Ordre final : CORS (extérieur) → maintenance → rate limit →
# métriques (intérieur). Ainsi les preflights OPTIONS sont répondues par CORS
# avant tout le reste et ne polluent ni les métriques ni le rate limit.
app.add_middleware(BaseHTTPMiddleware, dispatch=request_metrics_middleware)
app.add_middleware(BaseHTTPMiddleware, dispatch=rate_limit_middleware)
app.add_middleware(BaseHTTPMiddleware, dispatch=maintenance_mode_middleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allowed_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(train.router)
app.include_router(predict.router)
app.include_router(models.router)
app.include_router(maintenance.router)
app.include_router(metrics.router)
app.include_router(health.router)
app.include_router(ai_chat.router)
app.include_router(agent.router)
app.include_router(sessions.router)
app.include_router(evaluate.router)
app.include_router(drift.router)
app.include_router(explain.router)
app.include_router(pipeline.router)
app.include_router(active_learning.router)
app.include_router(classifiers.router)

# SCRUM-34 : démarre le scheduler APScheduler et recharge les planifications
# d'entraînement persistées (table scheduled_jobs du SQLite existant).
ensure_scheduler_started()

