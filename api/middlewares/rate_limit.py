import json
import math
import os
import threading
import time
from typing import Dict

from fastapi import Request
from fastapi.responses import Response

RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
_RATE_LIMIT_LOCK = threading.Lock()


class TokenBucket:
    def __init__(self, rate_per_minute: int):
        self.capacity = max(1, rate_per_minute)
        self.refill_rate = self.capacity / 60.0
        self.tokens = float(self.capacity)
        self.last_update = time.monotonic()

    def consume(self, amount: float = 1.0):
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


def _client_identifier(request: Request) -> str:
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
        return Response(
            content=json.dumps({"detail": "Rate limit exceeded. Please retry later."}),
            status_code=429,
            media_type="application/json",
            headers={"Retry-After": str(wait_seconds)},
        )
    return await call_next(request)
