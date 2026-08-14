# ThinkTuning — image Docker CPU (entraînement + inférence)
FROM python:3.13-slim

# Pas de .pyc, logs non bufferisés, caches HF/NLTK dans l'image (réutilisables via volume)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/app/.cache/huggingface \
    NLTK_DATA=/app/.cache/nltk \
    OMP_NUM_THREADS=4 \
    MKL_NUM_THREADS=4

WORKDIR /app

# build-essential : nécessaire si sentencepiece/tokenizers n'ont pas de wheel précompilé
# pour l'architecture cible (ex: certains hôtes ARM).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Couche dépendances séparée du code -> rebuild rapide quand seul le code change
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt

# Ressources NLTK utilisées par le module d'augmentation EDA (src/augmentation/eda.py)
RUN python -c "import nltk; [nltk.download(p, quiet=True) for p in ['wordnet', 'omw-1.4', 'omw-2.0', 'punkt', 'punkt_tab']]"

# Code du projet
COPY . .

# Dossiers de sortie (à monter en volume en pratique, créés ici par sécurité)
RUN mkdir -p experiments/checkpoints sentiment_model_final

# Entrypoint : gère train / api / both
ENTRYPOINT ["python", "entrypoint.py"]
CMD ["both"]

EXPOSE 8000