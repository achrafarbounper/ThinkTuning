# syntax=docker/dockerfile:1
# ============================================================================
# Image API ThinkTuning — PRODUCTION-READY (Phase 5, découplage déploiement)
#
# Contient UNIQUEMENT l'API FastAPI (le dashboard est une image séparée,
# cf. frontend/Dockerfile) :
#   - multi-stage : wheels compilés dans un builder jetable, runtime sans
#     toolchain (build-essential) ni cache pip ;
#   - gunicorn + workers uvicorn (supervision, redémarrage worker,
#     max-requests anti-fuite mémoire) — plus de process nu ;
#   - user non-root (1000) ;
#   - HEALTHCHECK sur /api/v1/health (public, exempté de maintenance) ;
#   - installation OFFLINE depuis le wheelhouse : build reproductible,
#     aucun accès index au stade runtime.
#
# NB mémoire : chaque worker importe torch/transformers au démarrage (~450 Mo
# RSS par worker) puis charge le modèle à la première prédiction (lazy, cf.
# core/predictor_cache). GUNICORN_WORKERS doit donc être dimensionné à la RAM
# disponible : défaut = 1, calibré pour les instances 512 Mo (Render Free /
# Starter). Sur un plan >= 2 Go, GUNICORN_WORKERS=2 redevient viable.
# ============================================================================

# ---------------------------------------------------------------------------
# 1/2 — WHEELHOUSE : compilation des wheels (toolchain jetable, cache pip)
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS wheelhouse

# build-essential : nécessaires uniquement pour les rares sdist sans wheel
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY requirements.txt .

# torch d'abord depuis l'index CPU officiel (wheel lourd, mis en cache pip)…
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --wheel-dir /wheelhouse torch --index-url https://download.pytorch.org/whl/cpu

# …puis le reste : torch est déjà dans le wheelhouse (pas de re-téléchargement).
RUN --mount=type=cache,target=/root/.cache/pip \
    pip wheel --wheel-dir /wheelhouse --find-links=/wheelhouse -r requirements.txt

# ---------------------------------------------------------------------------
# 2/2 — RUNTIME : installation offline, non-root, healthcheck, gunicorn
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

# OMP/MKL à 2 : les instances cloud gratuites partagent < 1 CPU — 4 threads
# n'apportent rien et gonflent la RAM (arenas BLAS par thread).
# GUNICORN_WORKERS=1 : calibré pour 512 Mo (Render Free/Starter). Chaque worker
# importe la stack ML au boot ; à 2 workers le conteneur dépasse 512 Mo ->
# OOM kill -> « No open HTTP ports detected » -> déploiement en échec.
# CLASSIFIER_WARMUP / MODEL_SANITY_ON_STARTUP à 0 : ne pas charger de modèle
# au démarrage (512 Mo ne peut de toute façon pas contenir le modèle de
# 541 Mo) ; /api/v1/health répond immédiatement et le sanity reste appelable
# via GET /health/model-sanity. Réactiver sur un plan >= 2 Go si souhaité.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/.cache/huggingface \
    NLTK_DATA=/app/.cache/nltk \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    GUNICORN_WORKERS=1 \
    MODEL_SANITY_ON_STARTUP=0 \
    CLASSIFIER_WARMUP=0

WORKDIR /app

# Dépendances depuis le wheelhouse : AUCUN accès réseau, build reproductible.
COPY --from=wheelhouse /wheelhouse /wheelhouse
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheelhouse -r requirements.txt \
    && rm -rf /wheelhouse

# Données NLTK (couche dédiée : ne s'invalide que si les deps changent)
RUN python -c "import nltk; [nltk.download(p, quiet=True) for p in ['wordnet', 'omw-1.4', 'omw-2.0', 'punkt', 'punkt_tab']]"

# Code applicatif (le .dockerignore exclut tests, modèles, caches…)
COPY . .
RUN mkdir -p /app/experiments/checkpoints /app/.cache \
    && chown -R 1000:1000 /app

USER 1000

EXPOSE 8000

# /api/v1/health : public (pas de clé requise), exempté du middleware de
# maintenance — le healthcheck reste meaningful même en mode maintenance.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4).read()"]

# sh -c + exec : GUNICORN_WORKERS paramétrable, exec remplace le shell (PID 1).
# max-requests : recyclage périodique des workers (anti-fuite longue durée).
# --no-control-socket : gunicorn >= 25.1 crée un socket unix de contrôle sous
#   $HOME (/.gunicorn/… sur Render) — refusé pour le user 1000 non-root
#   (« Control server error: [Errno 13] Permission denied: '/.gunicorn' »).
#   Inutile en conteneur (outil gunicornc non utilisé), on le désactive.
# -w : GUNICORN_WORKERS prioritaire, sinon WEB_CONCURRENCY (injecté par
#   Render), sinon 1.
CMD ["sh", "-c", "exec gunicorn api.main:app \
    -k uvicorn.workers.UvicornWorker \
    -w \"${GUNICORN_WORKERS:-${WEB_CONCURRENCY:-1}}\" \
    -b 0.0.0.0:8000 \
    --no-control-socket \
    --graceful-timeout 30 \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --access-logfile - \
    --error-logfile -"]