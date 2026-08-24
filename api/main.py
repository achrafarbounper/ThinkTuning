# project/api/main.py

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.routes import train, predict, maintenance, metrics, health, models, ai_chat, agent
from api.middlewares.maintenance import maintenance_mode_middleware
from api.middlewares.rate_limit import rate_limit_middleware
from api.middlewares.metrics import request_metrics_middleware

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
