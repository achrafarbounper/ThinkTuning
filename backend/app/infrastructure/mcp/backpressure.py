# project/app/infrastructure/mcp/backpressure.py
"""Backpressure MCP — garde-fou de capacité (L2 — SCRUM-153).

Un run ``orchestrate`` multi-agent consomme simultanément : un worker thread,
une connexion SSE longue (proxy : plusieurs minutes), un lease durable et des
appels LLM facturés. Sans plafond, un client (ou une boucle de réessai) peut
saturer la file de threads et le budget LLM — le mode de défaillance étant une
dégradation GLOBALE, silencieuse et non bornée.

Ce module borne donc l'admission à trois niveaux, avec un contrat HTTP
explicite plutôt qu'une panne par épuisement :

1. **``global``** : plafond des flux SSE ouverts, tous clients confondus
   (``MCP_MAX_CONCURRENT_STREAMS``) → ``503`` + ``Retry-After`` ;
2. **``client``** : plafond par ``client_id`` (``MCP_MAX_CONCURRENT_STREAMS_PER_CLIENT``)
   → ``503`` + ``Retry-After`` (un client ne peut pas monopoliser la capacité) ;
3. **``quota``** : débit d'OUVERTURE par client (``MCP_SSE_OPEN_RATE_PER_MINUTE``,
   réutilise ``TokenBucket`` — primitive partagée du rate limiting) → ``429``
   + ``Retry-After`` (« trop d'ouvertures », ≠ saturation de capacité).

Choix d'implémentation :

* ``BoundedSemaphore`` : acquisition NON bloquante (``blocking=False``) — un
  transport ne doit jamais mettre en attente une requête HTTP derrière un run
  de plusieurs minutes (la file d'attente serait un second vecteur de
  saturation). Soit une place est libre, soit on refuse tout de suite ;
* Compteur per-client plutôt que sémaphores par client : les sémaphores sont
  crées dynamiquement par ``client_id`` (cardinalité non bornée → fuite
  mémoire). Le compteur est purgé dès qu'il retombe à zéro ;
* ``TokenBucket`` partagé (``security/rate_limit_bucket``) : une seule
  implémentation de débit dans le projet, testée par le middleware REST ;
* Les valeurs par défaut sont LARGES (32 flux / 4 par client / 30 ouvertures
  par minute) : la contrainte doit rester invisible en usage nominal — elle
  n'existe que pour empêcher l'effondrement.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any

from app.infrastructure.mcp.mcp_events import DEGRADATION_BACKPRESSURE
from app.infrastructure.mcp.security.rate_limit_bucket import TokenBucket

# ---------------------------------------------------------------------------
# Vocabulaire (aligné sur les labels bornés de ``mcp_metrics``)
# ---------------------------------------------------------------------------

SCOPE_GLOBAL = "global"
SCOPE_CLIENT = "client"
SCOPE_QUOTA = "quota"

VALID_BACKPRESSURE_SCOPES: frozenset[str] = frozenset({SCOPE_GLOBAL, SCOPE_CLIENT, SCOPE_QUOTA})

#: Codes d'erreur MCP (stables, exploitables par les clients/UI).
CODE_BACKPRESSURE = "mcp_backpressure"
CODE_SSE_QUOTA = "mcp_sse_quota_exceeded"

STATUS_SERVICE_UNAVAILABLE = 503
STATUS_TOO_MANY_REQUESTS = 429

DEFAULT_MAX_CONCURRENT_STREAMS = 32
DEFAULT_MAX_CONCURRENT_STREAMS_PER_CLIENT = 4
DEFAULT_OPEN_RATE_PER_MINUTE = 30
DEFAULT_RETRY_AFTER_SECONDS = 2


def _env_int(name: str, default: int) -> int:
    """Lecture d'un entier d'environnement tolérante (valeur invalide → défaut)."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def max_concurrent_streams() -> int:
    return max(1, _env_int("MCP_MAX_CONCURRENT_STREAMS", DEFAULT_MAX_CONCURRENT_STREAMS))


def max_concurrent_streams_per_client() -> int:
    return max(
        1,
        _env_int(
            "MCP_MAX_CONCURRENT_STREAMS_PER_CLIENT",
            DEFAULT_MAX_CONCURRENT_STREAMS_PER_CLIENT,
        ),
    )


def open_rate_per_minute() -> int:
    return max(1, _env_int("MCP_SSE_OPEN_RATE_PER_MINUTE", DEFAULT_OPEN_RATE_PER_MINUTE))


@dataclass(frozen=True)
class CapacityRejection:
    """Refus d'admission explicite (jamais une exception : un contrat)."""

    scope: str
    message: str
    status_code: int = STATUS_SERVICE_UNAVAILABLE
    retry_after_seconds: int = DEFAULT_RETRY_AFTER_SECONDS
    code: str = CODE_BACKPRESSURE
    reason: str = DEGRADATION_BACKPRESSURE

    @property
    def headers(self) -> dict[str, str]:
        """En-têtes HTTP de refus (``Retry-After`` = contrat de réessai)."""
        return {"Retry-After": str(max(1, int(self.retry_after_seconds)))}

    def as_error_payload(self) -> dict[str, Any]:
        """Corps d'erreur JSON (même forme que les autres erreurs MCP)."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "scope": self.scope,
                "retry_after": max(1, int(self.retry_after_seconds)),
                "degraded": True,
                "reason": self.reason,
            }
        }

# ---------------------------------------------------------------------------
# Porte d'admission
# ---------------------------------------------------------------------------


class BackpressureGate:
    """Borne l'admission des flux MCP (global + par client + débit d'ouverture)."""

    def __init__(
        self,
        *,
        max_concurrent: int | None = None,
        max_concurrent_per_client: int | None = None,
        open_rate: int | None = None,
        retry_after_seconds: int = DEFAULT_RETRY_AFTER_SECONDS,
    ) -> None:
        self.max_concurrent = max(1, int(max_concurrent or max_concurrent_streams()))
        self.max_concurrent_per_client = max(
            1, int(max_concurrent_per_client or max_concurrent_streams_per_client())
        )
        self.open_rate = max(1, int(open_rate or open_rate_per_minute()))
        self.retry_after_seconds = max(1, int(retry_after_seconds))
        self._slots = threading.BoundedSemaphore(self.max_concurrent)
        self._lock = threading.Lock()
        self._active = 0
        self._per_client: dict[str, int] = {}
        self._buckets: dict[str, TokenBucket] = {}

    # -- lecture ------------------------------------------------------------
    @property
    def active_streams(self) -> int:
        with self._lock:
            return self._active

    def active_streams_for(self, client_id: str | None) -> int:
        key = _normalize_client(client_id)
        with self._lock:
            return self._per_client.get(key, 0)

    def snapshot(self) -> dict[str, Any]:
        """Vue sérialisable (diagnostic / tests)."""
        with self._lock:
            return {
                "active": self._active,
                "max_concurrent": self.max_concurrent,
                "max_concurrent_per_client": self.max_concurrent_per_client,
                "open_rate_per_minute": self.open_rate,
                "clients": dict(self._per_client),
            }

    # -- débit d'ouverture --------------------------------------------------
    def check_open_rate(self, client_id: str | None) -> CapacityRejection | None:
        """Consomme un jeton d'ouverture (``429`` si le client ouvre trop vite)."""
        key = _normalize_client(client_id)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(self.open_rate)
                self._buckets[key] = bucket
            allowed, wait_seconds = bucket.consume()
        if allowed:
            return None
        return CapacityRejection(
            scope=SCOPE_QUOTA,
            code=CODE_SSE_QUOTA,
            status_code=STATUS_TOO_MANY_REQUESTS,
            retry_after_seconds=max(1, wait_seconds or self.retry_after_seconds),
            message=(
                "Trop d'ouvertures de flux MCP pour ce client "
                f"({self.open_rate}/min) — réessayer après Retry-After."
            ),
        )

# -- admission ----------------------------------------------------------
    def try_acquire(self, client_id: str | None) -> CapacityRejection | None:
        """Tente de réserver une place de flux (``None`` = admission accordée).

        Ordre des contrôles : quota d'ouverture (le moins coûteux, protège
        aussi les refus en boucle), plafond client (équité), plafond global
        (survie du service). L'appelant DOIT appeler :meth:`release` si et
        seulement si cette méthode retourne ``None``.
        """
        quota_rejection = self.check_open_rate(client_id)
        if quota_rejection is not None:
            return quota_rejection
        key = _normalize_client(client_id)
        with self._lock:
            current = self._per_client.get(key, 0)
            if current >= self.max_concurrent_per_client:
                return CapacityRejection(
                    scope=SCOPE_CLIENT,
                    retry_after_seconds=self.retry_after_seconds,
                    message=(
                        "Capacité de flux MCP épuisée pour ce client "
                        f"({self.max_concurrent_per_client} simultanés) — "
                        "réessayer après Retry-After."
                    ),
                )
            if not self._slots.acquire(blocking=False):
                return CapacityRejection(
                    scope=SCOPE_GLOBAL,
                    retry_after_seconds=self.retry_after_seconds,
                    message=(
                        "Capacité globale de flux MCP épuisée "
                        f"({self.max_concurrent} simultanés) — "
                        "réessayer après Retry-After."
                    ),
                )
            self._active += 1
            self._per_client[key] = current + 1
            return None

    def release(self, client_id: str | None) -> None:
        """Libère la place réservée (idempotent : jamais de sur-libération)."""
        key = _normalize_client(client_id)
        with self._lock:
            if self._active <= 0:
                return
            self._active -= 1
            current = self._per_client.get(key, 0)
            if current <= 1:
                # Purge : la cardinalité du dict suit les clients ACTIFS.
                self._per_client.pop(key, None)
            else:
                self._per_client[key] = current - 1
        try:
            self._slots.release()
        except ValueError:  # pragma: no cover - défensif (jamais atteint)
            pass

    def reset(self) -> None:
        """Réinitialise l'état (tests) — aucun verrou résiduel."""
        with self._lock:
            self._active = 0
            self._per_client.clear()
            self._buckets.clear()
            self._slots = threading.BoundedSemaphore(self.max_concurrent)


def _normalize_client(client_id: str | None) -> str:
    return str(client_id or "").strip() or "anonymous"

# ---------------------------------------------------------------------------
# Singleton de transport (injectable pour les tests)
# ---------------------------------------------------------------------------

_gate: BackpressureGate | None = None
_gate_lock = threading.Lock()


def get_backpressure_gate() -> BackpressureGate:
    """Porte partagée du processus (paresseuse : aucune I/O)."""
    global _gate
    with _gate_lock:
        if _gate is None:
            _gate = BackpressureGate()
        return _gate


def configure_backpressure_gate(gate: BackpressureGate | None) -> None:
    """Injecte (ou réinitialise avec ``None``) la porte d'admission."""
    global _gate
    with _gate_lock:
        _gate = gate


__all__ = [
    "CODE_BACKPRESSURE",
    "CODE_SSE_QUOTA",
    "DEFAULT_MAX_CONCURRENT_STREAMS",
    "DEFAULT_MAX_CONCURRENT_STREAMS_PER_CLIENT",
    "DEFAULT_OPEN_RATE_PER_MINUTE",
    "DEFAULT_RETRY_AFTER_SECONDS",
    "SCOPE_CLIENT",
    "SCOPE_GLOBAL",
    "SCOPE_QUOTA",
    "STATUS_SERVICE_UNAVAILABLE",
    "STATUS_TOO_MANY_REQUESTS",
    "VALID_BACKPRESSURE_SCOPES",
    "BackpressureGate",
    "CapacityRejection",
    "configure_backpressure_gate",
    "get_backpressure_gate",
    "max_concurrent_streams",
    "max_concurrent_streams_per_client",
    "open_rate_per_minute",
]
