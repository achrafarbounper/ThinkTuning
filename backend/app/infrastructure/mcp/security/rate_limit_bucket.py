# project/app/infrastructure/mcp/security/rate_limit_bucket.py
"""Primitive ``TokenBucket`` — PARTAGÉE entre le middleware REST et l'enforceur MCP.

Intégration tâche 11 (docs/mcp/IMPLEMENTATION_PLAN.md) : le rate limiting
existant (``api/middlewares/rate_limit.py``) et l'enforceur de sécurité MCP
(``scope_enforcer.py``) utilisent LA MÊME implémentation de seau à jetons —
une seule primitive, deux consommateurs :

    - REST : clé ``client_id`` = IP client (``X-Forwarded-For`` si le proxy est
      fiable), limite globale ``RATE_LIMIT_PER_MINUTE`` ;
    - MCP  : clé ``client_id`` = identifiant du client MCP, limite per-client
      ``rate_limit_per_minute`` portée par ``MCPSecurityScope``.

La classe est déplacée ICI depuis ``api/middlewares/rate_limit.py`` (zéro
changement de comportement) et ré-exportée par le middleware pour compatibilité
(les tests REST continuent de viser ``api._reset_rate_limit_buckets`` etc.).
La primitive vit dans le paquet MCP pour que l'enforceur n'ait JAMAIS à importer
``api`` (lourd : le package api importe le stack HTTP + ML — la suite MCP doit
rester légère, cf. conftest).

   Sens de la dépendance : ``api/middlewares/rate_limit → app.infrastructure.mcp.security``
   jamais l'inverse.
"""

from __future__ import annotations

import math
import time


class TokenBucket:
    __slots__ = ("capacity", "refill_rate", "tokens", "last_update")

    def __init__(self, rate_per_minute: int) -> None:
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

        wait_seconds = (
            (amount - self.tokens) / self.refill_rate if self.refill_rate > 0 else 0.0
        )
        return False, max(1, int(math.ceil(wait_seconds)))


__all__ = ["TokenBucket"]
