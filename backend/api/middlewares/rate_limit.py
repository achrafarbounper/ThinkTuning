import os
import threading
import time

from fastapi import Request
from fastapi.responses import JSONResponse

# La primitive TokenBucket est PARTAGÉE avec l'enforceur de sécurité MCP
# (tâche 11 — app/infrastructure/mcp/security/rate_limit_bucket.py) : une seule
# implémentation pour deux consommateurs — REST (clé IP + RATE_LIMIT_PER_MINUTE
# global) et MCP (clé client_id + rate_limit_per_minute du scope client). Le
# middleware importe le paquet MCP ; l'enforceur n'importe JAMAIS `api` (import
# lourd : le package api charge le stack HTTP + ML — la suite MCP reste légère).
from app.infrastructure.mcp.security.rate_limit_bucket import TokenBucket

RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
# P0 SEC (F6) : X-Forwarded-For NON fiable par défaut (spoofable en exposition
# directe → bypass du throttle). Mettre 1 UNIQUEMENT derrière un reverse proxy
# de confiance (nginx compose : proxy_set_header X-Forwarded-For).
RATE_LIMIT_TRUST_PROXY = os.getenv("RATE_LIMIT_TRUST_PROXY", "0").lower() in {
    "1", "true", "yes", "on",
}

_RATE_LIMIT_LOCK = threading.Lock()

# Purge anti-fuite : chaque IP créait une entrée de bucket JAMAIS libérée
# (croissance mémoire illimitée sur une API exposée). Au-delà de
# _MAX_BUCKETS clients, on évacue les buckets inactifs.
_MAX_BUCKETS = 1024
_IDLE_SECONDS = 600.0


_RATE_LIMIT_BUCKETS: dict[str, TokenBucket] = {}


def _reset_rate_limit_buckets():
    """Used by tests to reset rate limit state."""
    with _RATE_LIMIT_LOCK:
        _RATE_LIMIT_BUCKETS.clear()


def _evict_idle_buckets_locked(now: float) -> None:
    """Purge les buckets inactifs (à appeler avec _RATE_LIMIT_LOCK posé)."""
    if len(_RATE_LIMIT_BUCKETS) < _MAX_BUCKETS:
        return
    stale = [
        key
        for key, bucket in _RATE_LIMIT_BUCKETS.items()
        if now - bucket.last_update > _IDLE_SECONDS
    ]
    for key in stale:
        del _RATE_LIMIT_BUCKETS[key]
    # Toujours saturé (clients actifs mais récents) : évacue les plus anciens.
    if len(_RATE_LIMIT_BUCKETS) >= _MAX_BUCKETS:
        oldest = sorted(_RATE_LIMIT_BUCKETS.items(), key=lambda item: item[1].last_update)
        for key, _bucket in oldest[: len(oldest) // 2]:
            del _RATE_LIMIT_BUCKETS[key]


def _client_identifier(request: Request) -> str:
    if RATE_LIMIT_TRUST_PROXY:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


def _enforce_rate_limit(request: Request):
    import api  # pour lire la valeur monkeypatchée

    rate = getattr(api, "RATE_LIMIT_PER_MINUTE", RATE_LIMIT_PER_MINUTE)

    if rate <= 0 or request.method.upper() != "POST":
        return None

    # Anti-DoS : /predict legacy ET /api/v1/predict (surface v1) partagent le
    # même token bucket — une limite distincte (ou absente) pour la v1 créerait
    # un contournement trivial pendant la migration. /predict/batch (multipart
    # CSV) est le point d'entrée le plus coûteux (upload + inférence) : inclus.
    if request.url.path not in {
        "/predict",
        "/predict/batch",
        "/compare",
        "/api/v1/predict",
        "/api/v1/predict/batch",
    }:
        return None

    client_id = _client_identifier(request)
    with _RATE_LIMIT_LOCK:
        _evict_idle_buckets_locked(time.monotonic())
        bucket = _RATE_LIMIT_BUCKETS.get(client_id)
        if bucket is None or bucket.capacity != max(1, rate):
            bucket = TokenBucket(rate)
            _RATE_LIMIT_BUCKETS[client_id] = bucket

        allowed, wait_seconds = bucket.consume(1.0)
        if allowed:
            return None
        return wait_seconds


async def rate_limit_middleware(request: Request, call_next):
    wait_seconds = _enforce_rate_limit(request)
    if wait_seconds is not None:
        return JSONResponse(
            status_code=429,
            content={"detail": "Rate limit exceeded. Please retry later."},
            headers={"Retry-After": str(wait_seconds)},
        )
    return await call_next(request)

