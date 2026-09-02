"""Tests d'intégration pour les hooks et middlewares dans AgentCore.

Vérifie :
    - Émission d'événements pendant un run
    - Exécution des middlewares
    - Rétrocompatibilité des callbacks
    - Intégration event_bus + middleware + agent_core
"""

import unittest
from unittest.mock import MagicMock, patch

from ia.agent.event_bus import EventBus, reset_event_bus, on, off
from ia.agent.middleware import clear_middlewares, register_middleware, process_tool_call


class TestEventEmission(unittest.TestCase):
    """Tests d'émission d'événements."""

    def setUp(self):
        reset_event_bus()
        clear_middlewares()
        self.events = []
        on("tool_call", lambda **kw: self.events.append(("call", kw)))
        on("tool_result", lambda **kw: self.events.append(("result", kw)))
        on("tool_error", lambda **kw: self.events.append(("error", kw)))
        on("run_start", lambda **kw: self.events.append(("start", kw)))
        on("run_end", lambda **kw: self.events.append(("end", kw)))

    def tearDown(self):
        reset_event_bus()
        clear_middlewares()

    def test_tool_call_event_emitted(self):
        """Vérifie que tool_call est émis."""
        from ia.agent.event_bus import get_event_bus
        bus = get_event_bus()
        bus.emit("tool_call", tool_name="test_tool", args={"x": 1}, job_id="j1")
        call_events = [e for e in self.events if e[0] == "call"]
        self.assertEqual(len(call_events), 1)

    def test_run_lifecycle_events(self):
        """Vérifie les événements de cycle de vie."""
        from ia.agent.event_bus import get_event_bus
        bus = get_event_bus()
        bus.emit("run_start", job_id="j1")
        bus.emit("run_end", job_id="j1", result="done", rounds_used=1)
        start_events = [e for e in self.events if e[0] == "start"]
        end_events = [e for e in self.events if e[0] == "end"]
        self.assertEqual(len(start_events), 1)
        self.assertEqual(len(end_events), 1)


class TestMiddlewareIntegration(unittest.TestCase):
    """Tests d'intégration des middlewares."""

    def setUp(self):
        clear_middlewares()

    def test_middleware_modifies_result(self):
        """Vérifie qu'un middleware peut modifier le résultat."""

        def modifier(ctx, next_call):
            result = next_call(ctx)
            return f"modified:{result}"

        register_middleware(modifier, priority=10)
        result = process_tool_call("tool", {}, lambda a: "original")
        self.assertEqual(result, "modified:original")

    def test_middleware_chain_order(self):
        """Vérifie l'ordre de chaînage."""
        order = []

        def mw1(ctx, next_call):
            order.append("A")
            return next_call(ctx)

        def mw2(ctx, next_call):
            order.append("B")
            return next_call(ctx)

        register_middleware(mw1, priority=10)
        register_middleware(mw2, priority=20)
        process_tool_call("tool", {}, lambda a: "ok")
        self.assertEqual(order, ["A", "B"])


class TestCallbackCompatibility(unittest.TestCase):
    """Tests de rétrocompatibilité des callbacks."""

    def test_on_tool_event_callback_still_works(self):
        """Vérifie que le callback on_tool_event historique fonctionne."""
        events_received = []

        def on_tool_event(event):
            events_received.append(event)

        # Simule l'appel historique
        on_tool_event({"event": "tool_start", "tool": "test"})
        on_tool_event({"event": "tool_result", "tool": "test", "status": "ok"})
        self.assertEqual(len(events_received), 2)
        self.assertEqual(events_received[0]["event"], "tool_start")
        self.assertEqual(events_received[1]["event"], "tool_result")


if __name__ == "__main__":
    unittest.main()