# project/api/main.py

import os
import sys

# --- Configuration logging ----------------------------------------------------
# Sans cette configuration, les loggers de l'agent (`agent.*`, cf. paquet ia/)
# n'ont AUCUN handler : Python n'affiche alors que les WARNING+ sur stderr via
# son handler « last resort », et uvicorn ne configure que ses propres loggers
# (`uvicorn`, `uvicorn.error`, `uvicorn.access`) — jamais ceux de votre app.
#
# On branche ici un handler CONSOLE COLORÉ (rich, cf. ia/logging_setup.py) sur
# la racine : tous les logs de l'API ET de l'agent s'affichent lisiblement dans
# le terminal (niveaux en couleur, durées, tracebacks riches). Idempotent :
# aucun doublon même si uvicorn recharge le module. Niveau réglable via la
# variable d'environnement AGENT_LOG_LEVEL (DEBUG/INFO/...).
IA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ia")
if IA_DIR not in sys.path:
    sys.path.insert(0, IA_DIR)

from logging_setup import setup_agent_logging  # noqa: E402

setup_agent_logging(os.getenv("AGENT_LOG_LEVEL", "INFO"))

from fastapi import FastAPI  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from api.routes import train, predict, maintenance, metrics, health, models, ai_chat, agent, sessions, evaluate, explain, drift  # noqa: E402
from api.middlewares.maintenance import maintenance_mode_middleware  # noqa: E402
from api.middlewares.rate_limit import rate_limit_middleware  # noqa: E402
from api.middlewares.metrics import request_metrics_middleware  # noqa: E402

CORS_ALLOWED_ORIGINS = [
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
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.middleware("http")(maintenance_mode_middleware)
app.middleware("http")(rate_limit_middleware)
app.middleware("http")(request_metrics_middleware)

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
