"""Tests unitaires pour le pattern Circuit Breaker.

Vérifie :
    - États du circuit breaker (CLOSED, OPEN, HALF_OPEN)
    - Transition vers OPEN après N échecs
    - Blocage des appels quand OPEN
    - Transition vers HALF_OPEN après timeout
    - Retour à CLOSED après succès en HALF_OPEN
    - Registre par outil
"""

import time
import unittest

from ia.agent.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerRegistry,
    get_circuit_breaker,
    get_registry,
    reset_registry,
)


class TestCircuitBreaker(unittest.TestCase):
    """Tests du Circuit Breaker."""

    def setUp(self):
        """Réinitialise le registre avant chaque test."""
        reset_registry()

    def test_initial_state_closed(self):
        """Vérifie que l'état initial est CLOSED."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=1.0)
        self.assertEqual(cb.state, "closed")
        self.assertTrue(cb.can_execute())

    def test_stays_closed_under_threshold(self):
        """Vérifie que le circuit reste CLOSED sous le seuil."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=1.0)
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, "closed")
        self.assertTrue(cb.can_execute())

    def test_opens_at_threshold(self):
        """Vérifie l'ouverture du circuit au seuil."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=1.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        self.assertEqual(cb.state, "open")
        self.assertFalse(cb.can_execute())

    def test_blocks_when_open(self):
        """Vérifie que les appels sont bloqués quand OPEN."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=10.0)
        cb.record_failure()
        self.assertFalse(cb.can_execute())

    def test_half_open_after_timeout(self):
        """Vérifie la transition vers HALF_OPEN après le timeout."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure()
        self.assertEqual(cb.state, "open")
        time.sleep(0.15)
        self.assertEqual(cb.state, "half_open")
        self.assertTrue(cb.can_execute())

    def test_closes_after_success_in_half_open(self):
        """Vérifie la fermure du circuit après un succès en HALF_OPEN."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure()
        time.sleep(0.15)
        self.assertEqual(cb.state, "half_open")
        cb.record_success()
        self.assertEqual(cb.state, "closed")
        self.assertTrue(cb.can_execute())

    def test_reopens_on_failure_in_half_open(self):
        """Vérifie la réouverture du circuit sur échec en HALF_OPEN."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.1)
        cb.record_failure()
        time.sleep(0.15)
        self.assertEqual(cb.state, "half_open")
        cb.record_failure()
        self.assertEqual(cb.state, "open")
        self.assertFalse(cb.can_execute())

    def test_success_resets_failure_count(self):
        """Vérifie qu'un succès réinitialise le compteur d'échecs."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=1.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        cb.record_failure()
        # Le compteur a été réinitialisé, donc on est encore à 2 échecs
        self.assertEqual(cb.state, "closed")

    def test_reset(self):
        """Vérifie la réinitialisation manuelle."""
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=1.0)
        cb.record_failure()
        self.assertEqual(cb.state, "open")
        cb.reset()
        self.assertEqual(cb.state, "closed")
        self.assertTrue(cb.can_execute())

    def test_metrics(self):
        """Vérifie les métriques exposées."""
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout=1.0)
        cb.record_failure()
        cb.record_success()
        metrics = cb.metrics
        self.assertEqual(metrics["failure_count"], 1)
        self.assertEqual(metrics["success_count"], 1)
        self.assertEqual(metrics["state"], "closed")
        self.assertEqual(metrics["failure_threshold"], 3)


class TestCircuitBreakerRegistry(unittest.TestCase):
    """Tests du registre de circuit breakers."""

    def setUp(self):
        reset_registry()

    def test_get_or_create(self):
        """Vérifie la création paresseuse."""
        reg = get_registry()
        cb1 = reg.get_or_create("tool_a")
        cb2 = reg.get_or_create("tool_a")
        self.assertIs(cb1, cb2)

    def test_different_tools_different_breakers(self):
        """Vérifie que chaque outil a son propre breaker."""
        reg = get_registry()
        cb1 = reg.get_or_create("tool_a")
        cb2 = reg.get_or_create("tool_b")
        self.assertIsNot(cb1, cb2)

    def test_remove(self):
        """Vérifie la suppression d'un breaker."""
        reg = get_registry()
        reg.get_or_create("tool_a")
        reg.remove("tool_a")
        cb = reg.get_or_create("tool_a")
        self.assertEqual(cb.state, "closed")

    def test_clear(self):
        """Vérifie le vidage du registre."""
        reg = get_registry()
        reg.get_or_create("tool_a")
        reg.get_or_create("tool_b")
        reg.clear()
        cb = reg.get_or_create("tool_a")
        self.assertEqual(cb.state, "closed")

    def test_global_get_circuit_breaker(self):
        """Vérifie la fonction globale get_circuit_breaker."""
        cb = get_circuit_breaker("my_tool")
        self.assertIsInstance(cb, CircuitBreaker)
        self.assertEqual(cb.state, "closed")


if __name__ == "__main__":
    unittest.main()