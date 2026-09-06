# syntax=docker/dockerfile:1
# ============================================================================
# Image API ThinkTuning — PRODUCTION-READY (Phase 5, découplage déploiement)
#
# Contient UNIQUEMENT l'API FastAPI (le dashboard est une image séparée,
# cf. dashboard/Dockerfile) :
#   - multi-stage : wheels compilés dans un builder jetable, runtime sans
#     toolchain (build-essential) ni cache pip ;
#   - gunicorn + workers uvicorn (supervision, redémarrage worker,
#     max-requests anti-fuite mémoire) — plus de process nu ;
#   - user non-root (1000) ;
#   - HEALTHCHECK sur /api/v1/health (public, exempté de maintenance) ;
#   - installation OFFLINE depuis le wheelhouse : build reproductible,
#     aucun accès index au stade runtime.
#
# NB mémoire : chaque worker charge le modèle à la première prédiction
# (lazy, cf. core/predictor_cache) — dimensionner GUNICORN_WORKERS à la RAM
# disponible (défaut : 2).
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

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/.cache/huggingface \
    NLTK_DATA=/app/.cache/nltk \
    OMP_NUM_THREADS=4 \
    MKL_NUM_THREADS=4 \
    GUNICORN_WORKERS=2

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
CMD ["sh", "-c", "exec gunicorn api.main:app \
    -k uvicorn.workers.UvicornWorker \
    -w \"${GUNICORN_WORKERS:-2}\" \
    -b 0.0.0.0:8000 \
    --graceful-timeout 30 \
    --max-requests 1000 \
    --max-requests-jitter 100 \
    --access-logfile - \
    --error-logfile -"]