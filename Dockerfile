# ============================
# 1) Build du frontend React/Vite
# ============================
FROM node:20-alpine AS frontend-build
WORKDIR /app/dashboard

COPY dashboard/package*.json ./
RUN npm ci

COPY dashboard/ .
ARG VITE_API_URL=http://localhost:8000
ENV VITE_API_URL=$VITE_API_URL
RUN npm run build


# ============================
# 2) Backend + Nginx + Supervisord
# ============================
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/.cache/huggingface \
    NLTK_DATA=/app/.cache/nltk \
    OMP_NUM_THREADS=4 \
    MKL_NUM_THREADS=4

WORKDIR /app

# Install system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        nginx \
        supervisor \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt

# Download NLTK data
RUN python -c "import nltk; [nltk.download(p, quiet=True) for p in ['wordnet', 'omw-1.4', 'omw-2.0', 'punkt', 'punkt_tab']]"

# Copy backend code
COPY . .
RUN mkdir -p experiments/checkpoints sentiment_model_final

# Copy frontend build into nginx
COPY --from=frontend-build /app/dashboard/dist /usr/share/nginx/html
COPY dashboard/nginx.conf /etc/nginx/conf.d/default.conf

# Copy supervisord config
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Create necessary directories
RUN mkdir -p /app/experiments /app/sentiment_model_final \
    && mkdir -p /var/log/supervisor \
    && mkdir -p /etc/nginx/conf.d

EXPOSE 80 8000

# Ensure supervisord runs in foreground
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
