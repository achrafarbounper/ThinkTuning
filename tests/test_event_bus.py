"""Tests unitaires pour le système d'événements EventBus.

Vérifie :
    - Enregistrement et désenregistrement de handlers
    - Émission synchrone et isolation d'erreurs
    - Handlers one-shot (once)
    - Comptage de listeners
    - Context manager temporary_listener
"""

import threading
import time
import unittest

from ia.agent.event_bus import (
    EventBus,
    get_event_bus,
    on,
    once,
    off,
    emit,
    listener_count,
    temporary_listener,
    reset_event_bus,
)


class TestEventBus(unittest.TestCase):
    """Tests de base de l'EventBus."""

    def setUp(self):
        """Réinitialise le singleton avant chaque test."""
        reset_event_bus()
        self.bus = EventBus()

    def test_on_emit_basic(self):
        """Vérifie l'enregistrement et l'émission basique."""
        results = []
        self.bus.on("test.event", lambda **kw: results.append(kw))
        self.bus.emit("test.event", key="value")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["key"], "value")
        self.assertEqual(results[0]["event_type"], "test.event")
        self.assertIn("timestamp", results[0])

    def test_multiple_handlers(self):
        """Vérifie que plusieurs handlers sont appelés."""
        calls = []
        self.bus.on("evt", lambda **kw: calls.append("a"))
        self.bus.on("evt", lambda **kw: calls.append("b"))
        self.bus.emit("evt")
        self.assertEqual(calls, ["a", "b"])

    def test_off_removes_handler(self):
        """Vérifie la désinscription d'un handler."""
        calls = []
        handler = lambda **kw: calls.append(1)
        self.bus.on("evt", handler)
        self.bus.emit("evt")
        self.assertEqual(len(calls), 1)
        self.bus.off("evt", handler)
        self.bus.emit("evt")
        self.assertEqual(len(calls), 1)  # Pas d'appel supplémentaire

    def test_once_auto_removes(self):
        """Vérifie qu'un handler once est supprimé après le premier appel."""
        calls = []
        self.bus.once("evt", lambda **kw: calls.append(1))
        self.bus.emit("evt")
        self.bus.emit("evt")
        self.assertEqual(len(calls), 1)

    def test_handler_error_isolation(self):
        """Vérifie qu'un handler en erreur ne bloque pas les autres."""
        calls = []

        def bad_handler(**kw):
            raise RuntimeError("boom")

        def good_handler(**kw):
            calls.append("ok")

        self.bus.on("evt", bad_handler)
        self.bus.on("evt", good_handler)
        # Ne doit pas lever d'exception
        self.bus.emit("evt")
        self.assertEqual(calls, ["ok"])

    def test_listener_count(self):
        """Vérifie le comptage de listeners."""
        h1 = lambda **kw: None
        h2 = lambda **kw: None
        self.bus.on("evt", h1)
        self.bus.on("evt", h2)
        self.assertEqual(self.bus.listener_count("evt"), 2)
        self.bus.off("evt", h1)
        self.assertEqual(self.bus.listener_count("evt"), 1)

    def test_clear_all_handlers(self):
        """Vide tous les handlers."""
        self.bus.on("evt1", lambda **kw: None)
        self.bus.on("evt2", lambda **kw: None)
        self.bus.clear()
        self.assertEqual(self.bus.listener_count("evt1"), 0)
        self.assertEqual(self.bus.listener_count("evt2"), 0)

    def test_clear_specific_event(self):
        """Vide les handlers d'un événement spécifique."""
        self.bus.on("evt1", lambda **kw: None)
        self.bus.on("evt2", lambda **kw: None)
        self.bus.clear("evt1")
        self.assertEqual(self.bus.listener_count("evt1"), 0)
        self.assertEqual(self.bus.listener_count("evt2"), 1)

    def test_no_handlers_no_error(self):
        """Vérifie qu'émettre sans handler ne lève pas d'erreur."""
        self.bus.emit("nonexistent.event", key="value")  # Ne lève rien

    def test_temporary_listener(self):
        """Vérifie le context manager temporary_listener."""
        calls = []
        handler = lambda **kw: calls.append(1)
        with temporary_listener("evt", handler):
            emit("evt")
        emit("evt")  # En dehors du contexte
        self.assertEqual(len(calls), 1)

    def test_singleton_get_event_bus(self):
        """Vérifie que get_event_bus retourne toujours la même instance."""
        bus1 = get_event_bus()
        bus2 = get_event_bus()
        self.assertIs(bus1, bus2)

    def test_reset_event_bus(self):
        """Vérifie la réinitialisation du singleton."""
        bus1 = get_event_bus()
        reset_event_bus()
        bus2 = get_event_bus()
        self.assertIsNot(bus1, bus2)

    def test_global_on_off_emit(self):
        """Vérifie les fonctions globales on/off/emit."""
        calls = []
        handler = lambda **kw: calls.append(kw.get("x"))
        on("test.global", handler)
        emit("test.global", x=42)
        self.assertEqual(calls, [42])
        off("test.global", handler)
        emit("test.global", x=99)
        self.assertEqual(calls, [42])

    def test_thread_safety(self):
        """Vérifie la thread-safety de base."""
        calls = []
        lock = threading.Lock()

        def handler(**kw):
            with lock:
                calls.append(kw)

        self.bus.on("evt", handler)
        threads = [threading.Thread(target=lambda: self.bus.emit("evt", i=i)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(calls), 10)


if __name__ == "__main__":
    unittest.main()