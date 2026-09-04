import math
import os
import threading
import time
from typing import Dict

from fastapi import Request
from fastapi.responses import JSONResponse

RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
# Derrière un reverse proxy de confiance (nginx en prod), l'IP client arrive
# dans X-Forwarded-For. Désactiver (0) si l'API est exposée directement :
# sinon un client peut forger son IP et contourner la limite.
RATE_LIMIT_TRUST_PROXY = os.getenv("RATE_LIMIT_TRUST_PROXY", "1").lower() in {
    "1", "true", "yes", "on",
}

_RATE_LIMIT_LOCK = threading.Lock()

# Purge anti-fuite : chaque IP créait une entrée de bucket JAMAIS libérée
# (croissance mémoire illimitée sur une API exposée). Au-delà de
# _MAX_BUCKETS clients, on évacue les buckets inactifs.
_MAX_BUCKETS = 1024
_IDLE_SECONDS = 600.0


class TokenBucket:
    __slots__ = ("capacity", "refill_rate", "tokens", "last_update")

    def __init__(self, rate_per_minute: int):
        self.capacity = max(1, rate_per_minute)
        self.refill_rate = self.capacity / 60.0
        self.tokens = float(self.capacity)
        self.last_update = time.monotonic()

    def consume(self, amount: float = 1.0) -> tuple[bool, int]:
        now = time.monotonic()
        elapsed = now - self.last_update
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_update = now

        if self.tokens >= amount:
            self.tokens -= amount
            return True, 0

        wait_seconds = (amount - self.tokens) / self.refill_rate if self.refill_rate > 0 else 0.0
        return False, max(1, int(math.ceil(wait_seconds)))


_RATE_LIMIT_BUCKETS: Dict[str, TokenBucket] = {}


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

    if request.url.path not in {"/predict", "/predict/batch", "/compare"}:
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

