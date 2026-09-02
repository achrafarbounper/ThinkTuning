"""Pattern Circuit Breaker pour les appels d'outils externes.

Protège l'agent contre les pannes en cascade en bloquant temporairement
les appels à un outil qui échoue répétitivement.

États :
    - CLOSED   : fonctionnement normal, les appels sont autorisés
    - OPEN     : l'outil est bloqué, les appels sont rejetés immédiatement
    - HALF_OPEN : test de rétablissement, un seul autorisé
"""

import logging
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger("thinktuning.agent.circuit_breaker")


class CircuitState:
    """États possibles du circuit breaker."""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Circuit breaker par outil."""

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._state = CircuitState.CLOSED
        self._failure_count = 0  # Total des échecs (jamais remis à zéro)
        self._consecutive_failures = 0  # Échecs consécututifs (remis à zéro par succès)
        self._success_count = 0
        self._last_failure_time: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            self._check_recovery()
            return self._state

    def can_execute(self) -> bool:
        with self._lock:
            self._check_recovery()
            return self._state != CircuitState.OPEN

    def record_success(self) -> None:
        """Enregistre un succès: réinitialise les échecs consécututifs."""
        with self._lock:
            self._consecutive_failures = 0
            self._success_count += 1
            self._state = CircuitState.CLOSED
            logger.debug("circuit_breaker: success, state=CLOSED")

    def record_failure(self) -> None:
        """Enregistre un échec: incrémente le compteur et ouvre si nécessaire."""
        with self._lock:
            self._failure_count += 1
            self._consecutive_failures += 1
            self._success_count = 0
            self._last_failure_time = time.time()
            if self._consecutive_failures >= self._failure_threshold:
                self._state = CircuitState.OPEN
                logger.warning(
                    "circuit_breaker: OPEN after %d failures",
                    self._consecutive_failures,
                )

    def _check_recovery(self) -> None:
        if self._state == CircuitState.OPEN:
            elapsed = time.time() - self._last_failure_time
            if elapsed >= self._recovery_timeout:
                self._state = CircuitState.HALF_OPEN
                logger.info("circuit_breaker: OPEN -> HALF_OPEN after %.1fs", elapsed)

    def reset(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._consecutive_failures = 0
            self._success_count = 0
            self._state = CircuitState.CLOSED

    @property
    def metrics(self) -> Dict[str, any]:
        with self._lock:
            return {
                "state": self._state,
                "failure_count": self._failure_count,
                "success_count": self._success_count,
                "failure_threshold": self._failure_threshold,
            }


# ============================================================
# Registre global
# ============================================================

class CircuitBreakerRegistry:
    """Registre de circuit breakers par outil."""

    def __init__(self) -> None:
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._lock = threading.Lock()

    def get_or_create(
        self,
        tool_name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
    ) -> CircuitBreaker:
        with self._lock:
            if tool_name not in self._breakers:
                self._breakers[tool_name] = CircuitBreaker(
                    failure_threshold=failure_threshold,
                    recovery_timeout=recovery_timeout,
                )
            return self._breakers[tool_name]

    def remove(self, tool_name: str) -> None:
        with self._lock:
            self._breakers.pop(tool_name, None)

    def clear(self) -> None:
        with self._lock:
            self._breakers.clear()

    def get_all_states(self) -> Dict[str, str]:
        with self._lock:
            return {name: cb.state for name, cb in self._breakers.items()}


_registry: Optional[CircuitBreakerRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> CircuitBreakerRegistry:
    """Retourne le registre singleton."""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = CircuitBreakerRegistry()
    return _registry


def reset_registry() -> None:
    """Réinitialise le registre (pour les tests)."""
    global _registry
    with _registry_lock:
        _registry = None


def get_circuit_breaker(
    tool_name: str,
    failure_threshold: int = 5,
    recovery_timeout: float = 60.0,
) -> CircuitBreaker:
    """Retourne (ou crée) le circuit breaker pour un outil."""
    return get_registry().get_or_create(
        tool_name,
        failure_threshold=failure_threshold,
        recovery_timeout=recovery_timeout,
    )


def reset_circuit_breakers() -> None:
    """Réinitialise tous les circuit breakers."""
    get_registry().clear()


def get_all_states() -> Dict[str, str]:
    """Retourne l'état de tous les circuit breakers."""
    return get_registry().get_all_states()