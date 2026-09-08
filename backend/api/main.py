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

import threading  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

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

    from core.model_sanity import VERDICT_OK, run_model_sanity
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
    # MODEL_SANITY_ON_STARTUP=0 : démarrage sans toucher aux poids (petites
    # instances 512 Mo, cf. Dockerfile) ; le sanity reste disponible à la
    # demande via GET /health/model-sanity.
    if os.getenv("MODEL_SANITY_ON_STARTUP", "1") != "0":
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


from api.middlewares.maintenance import maintenance_mode_middleware  # noqa: E402
from api.middlewares.metrics import request_metrics_middleware  # noqa: E402
from api.middlewares.rate_limit import rate_limit_middleware  # noqa: E402
from core.scheduler import ensure_scheduler_started  # noqa: E402


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

def _cors_allow_origin_regex() -> str | None:
    """Regex d'origines CORS additionnelles (optionnelle).

    Utile quand les origines déployées ne sont pas fixes — ex. previews Vercel
    (« think-tuning-ai-<hash>-snowy-two-23.vercel.app »), dont le sous-domaine
    change à chaque déploiement et ne peut pas être listé en CSV. Vide (défaut) :
    aucune origine regex, seules les origines explicites de
    CORS_ALLOWED_ORIGINS sont acceptées (comportement inchangé).
    Ex. (préfixé par le nom de projet Vercel « think-tuning-ai ») :
    CORS_ALLOW_ORIGIN_REGEX=^https://think-tuning-ai-[a-z0-9-]+\\.vercel\\.app$
    """
    raw = os.getenv("CORS_ALLOW_ORIGIN_REGEX", "").strip()
    return raw or None



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
    allow_origin_regex=_cors_allow_origin_regex(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# SCRUM-34 : démarre le scheduler APScheduler et recharge les planifications
# d'entraînement persistées (table scheduled_jobs du SQLite existant).
ensure_scheduler_started()

# === API v1 versionnée (découplage frontend/backend — strangler pattern) ===
# Surface stable consommable par le dashboard : les routes v1 passent par
# les ports + use-cases (app/domain, app/application) au lieu des modules
# core.* directs. Les routes legacy ci-dessus restent servies en parallèle ;
# la couche legacy sera retirée endpoint par endpoint une fois la v1 validée.
from api.dependencies.composition import container  # noqa: E402
from api.errors import register_domain_error_handlers  # noqa: E402
from api.routes.v1 import router as v1_router  # noqa: E402

# Composition root : enregistre les adaptateurs par défaut (paresseux —
# aucun modèle n'est chargé ici, uniquement des factories).
container.bootstrap()

# Mapping global DomainError -> réponses HTTP ({"error": {"code", ...}}).
register_domain_error_handlers(app)

app.include_router(v1_router, prefix="/api/v1")

# === MCP Server Layer (S1 — Bootstrap, docs/mcp/IMPLEMENTATION_PLAN.md) ===
# Surface MCP montée en parallèle de l'API REST : les clients MCP
# (Claude Desktop, Cursor…) s'adressent à ``POST /mcp/sse`` sans passer par le
# découplage /api/v1. Interrupteur de rollback : ``MCP_SERVER_ENABLED=false``
# → 503 (le serveur MCP est désactivé sans toucher au reste de l'API).
from app.infrastructure.mcp.mcp_server_sse import router as mcp_router  # noqa: E402

app.include_router(mcp_router)

