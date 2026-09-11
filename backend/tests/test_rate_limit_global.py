"""Tests P1 SEC — quotas PAR ROUTE COÛTEUSE (``api/middlewares/rate_limit``).

Table ``COSTLY_ROUTE_LIMITS`` (IP × route), fenêtre arbitraire (min/heure),
WARNING par 429, bucket global /predict* préservé, ``RedisTokenBucket``
fail-open. 100 % offline (aucun serveur, aucune requête HTTP).

Lance avec : pytest tests/test_rate_limit_global.py -v
"""

import logging

from api.middlewares import rate_limit as rl
from api.middlewares.rate_limit import (
    RedisTokenBucket,
    _match_costly_route,
    _reset_rate_limit_buckets,
)


def setup_function(_function) -> None:
    _reset_rate_limit_buckets()


# --- Table de routage --------------------------------------------------------------


def test_match_ask_group() -> None:
    assert _match_costly_route("/api/agent/ask/core") == ("agent_ask", 20, 60)
    assert _match_costly_route("/api/agent/ask/core/stream") == ("agent_ask", 20, 60)
    assert _match_costly_route("/api/v1/agent/ask/core") == ("agent_ask", 20, 60)


def test_match_multi_group_takes_priority_over_ask() -> None:
    # Ordre de la table : /multi/ask DOIT matcher AVANT /ask (prefixe).
    assert _match_costly_route("/api/agent/multi/ask") == ("agent_multi", 30, 60)
    assert _match_costly_route("/api/agent/multi/ask/stream") == ("agent_multi", 30, 60)


def test_match_hourly_groups() -> None:
    assert _match_costly_route("/train") == ("train", 5, 3600)
    assert _match_costly_route("/api/v1/train") == ("train", 5, 3600)
    assert _match_costly_route("/pipeline") == ("pipeline", 5, 3600)
    assert _match_costly_route("/api/v1/pipeline/cancel/x") == ("pipeline", 5, 3600)


def test_match_mcp_group() -> None:
    assert _match_costly_route("/mcp/sse") == ("mcp_orchestrate", 30, 60)


def test_match_predict_not_costly_bucket() -> None:
    # /predict* reste dans le bucket GLOBAL historique (pas de double quota).
    assert _match_costly_route("/predict") is None
    assert _match_costly_route("/api/v1/predict") is None


def test_unmatched_route_has_no_quota() -> None:
    assert _match_costly_route("/health") is None
    assert _match_costly_route("/agent/tools") is None


# --- Consommation (mémoire locale) -------------------------------------------------


def test_quota_blocks_after_limit() -> None:
    for _ in range(2):  # limite testée = 2/min
        assert rl._consume_costly("1.2.3.4", "agent_ask", 2, 60) is None
    # 3e appel au-delà de la limite de 2 : bloqué avec un retry-after.
    wait = rl._consume_costly("1.2.3.4", "agent_ask", 2, 60)
    assert isinstance(wait, int) and wait >= 1


def test_quota_is_per_client_and_per_group() -> None:
    for _ in range(2):
        rl._consume_costly("1.2.3.4", "agent_ask", 2, 60)
    # Autre IP : son propre budget.
    assert rl._consume_costly("5.6.7.8", "agent_ask", 2, 60) is None
    # Même IP, autre groupe : budget séparé.
    assert rl._consume_costly("1.2.3.4", "explain", 2, 60) is None


def test_hourly_window_refills_slowly() -> None:
    # Quota 1/heure épuisé → retry-after proche d'une HEURE (pas d'1 minute).
    assert rl._consume_costly("9.9.9.9", "train", 1, 3600) is None
    wait = rl._consume_costly("9.9.9.9", "train", 1, 3600)
    assert wait >= 3000  # ~3600s moins le refill minuscule


def test_five_per_hour_bucket_wait_is_hours_scale() -> None:
    # Quota 5/heure épuisé : le 6e appel attend ~720s (refill 5/3600 par s),
    # soit une échelle HORAIRE — très loin du refill minute du bucket global.
    for _ in range(5):
        assert rl._consume_costly("8.8.8.8", "train", 5, 3600) is None
    wait = rl._consume_costly("8.8.8.8", "train", 5, 3600)
    assert wait >= 600  # ~720s (vs ~60s max pour une limite minute)


# --- WARNING structuré sur 429 ------------------------------------------------------


def test_warning_logged_on_exceeded(caplog, monkeypatch) -> None:
    # Table réduite à une route fictive (limite 1) pour épuiser en 2 appels
    # via l'ENFORCEUR (le WARNING est émis là, pas dans _consume_costly).
    monkeypatch.setattr(
        rl, "COSTLY_ROUTE_LIMITS", (("/busy", "busy_grp", 1, 60),)
    )
    monkeypatch.setattr(rl, "_REDIS_CLIENT", None, raising=False)
    first = rl._enforce_rate_limit(_FakeRequest("/busy"))
    assert first is None
    with caplog.at_level(logging.WARNING, logger="api.middlewares.rate_limit"):
        wait = rl._enforce_rate_limit(_FakeRequest("/busy"))
    assert wait is not None
    msgs = [r.getMessage() for r in caplog.records if "rate_limit_exceeded" in r.getMessage()]
    assert msgs and "busy_grp" in msgs[0] and "10.0.0.7" in msgs[0]


# --- RedisTokenBucket (fail-open + fixed window) ------------------------------------


class _FakeRedis:
    """Redis minimal en mémoire : INCRBY/TTL/EXPIRE via pipeline."""

    def __init__(self) -> None:
        self.data: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    def pipeline(self):
        return _FakePipeline(self)

    def expire(self, key: str, ttl: int) -> None:
        self.ttls[key] = ttl


class _FakePipeline:
    def __init__(self, redis: _FakeRedis) -> None:
        self._redis = redis
        self._ops: list[tuple[str, str, int]] = []

    def incrby(self, key: str, amount: int):
        self._ops.append(("incrby", key, amount))
        return self

    def ttl(self, key: str):
        self._ops.append(("ttl", key, 0))
        return self

    def execute(self):
        results = []
        for op, key, amount in self._ops:
            if op == "incrby":
                self._redis.data[key] = self._redis.data.get(key, 0) + amount
                results.append(self._redis.data[key])  # redis-py : 1 résultat/cmd
            else:
                results.append(self._redis.ttls.get(key, -1))
        return results


def test_redis_bucket_fixed_window() -> None:
    client = _FakeRedis()
    bucket = RedisTokenBucket(client, "k", capacity=2, window_seconds=60)
    assert bucket.consume() == (True, 0)
    assert bucket.consume() == (True, 0)
    allowed, wait = bucket.consume()
    assert not allowed and 0 < wait <= 60


def test_redis_bucket_fails_open_on_error() -> None:
    class _Broken:
        def pipeline(self):
            raise ConnectionError("redis down")

    bucket = RedisTokenBucket(_Broken(), "k", capacity=1, window_seconds=60)
    # Redis down → PAS de blocage (fail-open documenté).
    assert bucket.consume() == (True, 0)


# --- Enforceur bout-en-bout (Request factice) ---------------------------------------


class _FakeRequest:
    def __init__(self, path: str, method: str = "POST") -> None:
        self.url = type("U", (), {"path": path})()
        self.method = method
        self.client = type("C", (), {"host": "10.0.0.7"})()


def test_enforce_blocks_sixth_train_per_hour(monkeypatch) -> None:
    monkeypatch.setattr(rl, "_REDIS_CLIENT", None, raising=False)
    for _ in range(5):
        assert rl._enforce_rate_limit(_FakeRequest("/train")) is None
    wait = rl._enforce_rate_limit(_FakeRequest("/train"))
    assert wait is not None and wait >= 600  # ~720s : échelle horaire
    # GET /train/{id} n'est PAS concerné (POST only).
    assert rl._enforce_rate_limit(_FakeRequest("/train", method="GET")) is None


def test_enforce_predict_bucket_untouched(monkeypatch) -> None:
    monkeypatch.setattr(rl, "_REDIS_CLIENT", None, raising=False)
    # Le bucket global /predict* continue de s'appliquer indépendamment.
    assert rl._enforce_rate_limit(_FakeRequest("/predict")) is None
    # Et /api/agent/ask a SON propre quota sans toucher /predict.
    assert rl._enforce_rate_limit(_FakeRequest("/api/agent/ask/core")) is None
