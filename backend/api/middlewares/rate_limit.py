import logging
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

logger = logging.getLogger(__name__)

RATE_LIMIT_PER_MINUTE = int(os.getenv("RATE_LIMIT_PER_MINUTE", "60"))
# P0 SEC (F6) : X-Forwarded-For NON fiable par défaut (spoofable en exposition
# directe → bypass du throttle). Mettre 1 UNIQUEMENT derrière un reverse proxy
# de confiance (nginx compose : proxy_set_header X-Forwarded-For).
RATE_LIMIT_TRUST_PROXY = os.getenv("RATE_LIMIT_TRUST_PROXY", "0").lower() in {
    "1", "true", "yes", "on",
}

# --- P1 SEC (point 12) : quotas PAR ROUTE COÛTEUSE --------------------------------
# Chaque route consommatrice (LLM, GPU, sous-processus) a son propre budget
# par client, indépendant du bucket global /predict* (conservé pour compat).
# Table : (préfixe de chemin, groupe, limite, fenêtre en secondes).
# Un seul groupe par requête (première correspondance) : les quotas ne
# se cumulent pas.
COSTLY_ROUTE_LIMITS: tuple[tuple[str, str, int, int], ...] = (
    # Inscription publique : route SANS authentification — quota par IP serré
    # contre la création de comptes en masse / l'énumération d'emails.
    ("/api/v1/auth/register", "auth_register", 10, 60),
    # Multi-agents : coordination Lead/Workers (LLM x N) — 30/min.
    ("/api/agent/multi/ask", "agent_multi", 30, 60),
    ("/api/v1/agent/multi/ask", "agent_multi", 30, 60),
    # Ask (noyau v2 + stream) : un run LLM par appel — 20/min.
    ("/api/agent/ask", "agent_ask", 20, 60),
    ("/api/v1/agent/ask", "agent_ask", 20, 60),
    # Explication LLM (probing) — 30/min.
    ("/explain", "explain", 30, 60),
    ("/api/v1/explain", "explain", 30, 60),
    # Entraînement & pipeline : GPU, minutes d'exécution — 5/HEURE.
    ("/train", "train", 5, 3600),
    ("/api/v1/train", "train", 5, 3600),
    ("/pipeline", "pipeline", 5, 3600),
    ("/api/v1/pipeline", "pipeline", 5, 3600),
    # Transport MCP (POST /mcp/sse : tools/call, orchestrate…) — 30/min.
    ("/mcp", "mcp_orchestrate", 30, 60),
)

# Stockage optionnel partagé (multi-réplicas) : REDIS_URL active un seau Redis
# (import paresseux — redis n'est PAS une dépendance dure du projet).
REDIS_URL = os.getenv("RATE_LIMIT_REDIS_URL", "").strip()

_RATE_LIMIT_LOCK = threading.Lock()

# Purge anti-fuite : chaque IP créait une entrée de bucket JAMAIS libérée
# (croissance mémoire illimitée sur une API exposée). Au-delà de
# _MAX_BUCKETS clients, on évacue les buckets inactifs.
_MAX_BUCKETS = 1024
_IDLE_SECONDS = 600.0

_RATE_LIMIT_BUCKETS: dict[str, TokenBucket] = {}
# Buckets des routes coûteuses, clé = (client_id, groupe).
_COSTLY_BUCKETS: dict[tuple[str, str], TokenBucket] = {}


def _reset_rate_limit_buckets():
    """Used by tests to reset rate limit state."""
    with _RATE_LIMIT_LOCK:
        _RATE_LIMIT_BUCKETS.clear()
        _COSTLY_BUCKETS.clear()


def _evict_idle_buckets_locked(now: float) -> None:
    """Purge les buckets inactifs (à appeler avec _RATE_LIMIT_LOCK posé)."""
    for store in (_RATE_LIMIT_BUCKETS, _COSTLY_BUCKETS):
        if len(store) < _MAX_BUCKETS:
            continue
        stale = [
            key
            for key, bucket in store.items()
            if now - bucket.last_update > _IDLE_SECONDS
        ]
        for key in stale:
            del store[key]
        # Toujours saturé (clients actifs mais récents) : évacue les plus anciens.
        if len(store) >= _MAX_BUCKETS:
            oldest = sorted(store.items(), key=lambda item: item[1].last_update)
            for key, _bucket in oldest[: len(oldest) // 2]:
                del store[key]


class RedisTokenBucket:
    """Seau à jetons FIXED-WINDOW adossé à Redis (multi-réplicas, P1 point 12).

    Sémantique volontairement simple (INCR + EXPIRE atomiques) : suffisante
    pour un quota anti-abus ; pas une comptabilité exacte type GCRA. En cas
    d'erreur Redis, on ÉCHOUE OUVERT (fail-open) : une panne de cache ne doit
    pas couper l'API — le WARNING loggé alerte l'exploitation.
    """

    __slots__ = ("capacity", "window_seconds", "key", "_redis", "last_update")

    def __init__(self, client, key: str, capacity: int, window_seconds: int) -> None:
        self._redis = client
        self.key = key
        self.capacity = max(1, capacity)
        self.window_seconds = max(1, window_seconds)
        self.last_update = time.monotonic()  # compat purge _evict_idle

    def consume(self, amount: float = 1.0) -> tuple[bool, int]:
        try:
            pipe = self._redis.pipeline()
            pipe.incrby(self.key, int(amount))
            pipe.ttl(self.key)
            # redis-py renvoie UN résultat par commande empilée : [incrby, ttl].
            count, ttl = pipe.execute()
            if count == int(amount):  # première requête de la fenêtre
                self._redis.expire(self.key, self.window_seconds)
                return True, 0
            if count > self.capacity:
                retry = int(ttl) if ttl and int(ttl) > 0 else self.window_seconds
                return False, retry
            return True, 0
        except Exception as exc:  # fail-open : Redis down ne coupe pas l'API
            logger.warning(
                "rate_limit_redis_unavailable key=%s error=%s (fail-open)",
                self.key, exc,
            )
            return True, 0


def _get_redis_client():
    """Client Redis paresseux (None si REDIS_URL absent ou import impossible)."""
    if not REDIS_URL:
        return None
    try:
        import redis  # type: ignore[import-not-found]  # dépendance optionnelle
    except ImportError:
        logger.warning(
            "RATE_LIMIT_REDIS_URL défini mais le package 'redis' est absent : "
            "limites en mémoire locale (par réplica)."
        )
        return None
    try:
        return redis.Redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as exc:  # pragma: no cover - config invalide
        logger.warning("Redis injoignable (%s) : limites en mémoire locale.", exc)
        return None


# Client Redis unique (créé à l'import ; None par défaut — mémoire locale).
_REDIS_CLIENT = _get_redis_client()


def _match_costly_route(path: str) -> tuple[str, int, int] | None:
    """Groupe (limite, fenêtre) de la première route coûteuse qui correspond."""
    for prefix, group, limit, window in COSTLY_ROUTE_LIMITS:
        if path == prefix or path.startswith(prefix):
            return group, limit, window
    return None


def _consume_costly(client_id: str, group: str, limit: int, window: int) -> int | None:
    """Consomme 1 jeton du quota (client, groupe). Retourne le retry-after ou None."""
    if _REDIS_CLIENT is not None:
        bucket: RedisTokenBucket | TokenBucket = RedisTokenBucket(
            _REDIS_CLIENT, f"ratelimit:{group}:{client_id}", limit, window
        )
        allowed, wait = bucket.consume(1.0)
        return None if allowed else wait

    with _RATE_LIMIT_LOCK:
        _evict_idle_buckets_locked(time.monotonic())
        bucket = _COSTLY_BUCKETS.get((client_id, group))
        if bucket is None or bucket.capacity != max(1, limit):
            bucket = TokenBucket(limit)
            bucket.refill_rate = limit / max(1, window)  # fenêtre arbitraire (ex : 1 h)
            _COSTLY_BUCKETS[(client_id, group)] = bucket
        allowed, wait = bucket.consume(1.0)
        return None if allowed else wait


def _client_identifier(request: Request) -> str:
    if RATE_LIMIT_TRUST_PROXY:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip()
    if request.client is not None:
        return request.client.host
    return "unknown"


def _enforce_rate_limit(request: Request):
    if request.method.upper() != "POST":
        return None

    path = request.url.path
    client_id = _client_identifier(request)

    # 1) Quotas par route coûteuse (P1 point 12) — indépendants du bucket global.
    matched = _match_costly_route(path)
    if matched is not None:
        group, limit, window = matched
        wait = _consume_costly(client_id, group, limit, window)
        if wait is not None:
            logger.warning(
                "rate_limit_exceeded group=%s client=%s path=%s retry_after=%ss",
                group, client_id, path, wait,
            )
            return wait
        return None

    # 2) Bucket global /predict* (historique F6, conservé pour compat).
    if path not in {
        "/predict",
        "/predict/batch",
        "/compare",
        "/api/v1/predict",
        "/api/v1/predict/batch",
    }:
        return None

    import api  # pour lire la valeur monkeypatchée
    rate = getattr(api, "RATE_LIMIT_PER_MINUTE", RATE_LIMIT_PER_MINUTE)
    if rate <= 0:
        return None

    with _RATE_LIMIT_LOCK:
        _evict_idle_buckets_locked(time.monotonic())
        bucket = _RATE_LIMIT_BUCKETS.get(client_id)
        if bucket is None or bucket.capacity != max(1, rate):
            bucket = TokenBucket(rate)
            _RATE_LIMIT_BUCKETS[client_id] = bucket

        allowed, wait_seconds = bucket.consume(1.0)
        if allowed:
            return None
        logger.warning(
            "rate_limit_exceeded group=predict client=%s path=%s retry_after=%ss",
            client_id, path, wait_seconds,
        )
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

