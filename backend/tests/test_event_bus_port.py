"""Tests de la conformité ``EventBusPort`` et de ses deux implémentations.

- ``test_event_bus_port.py`` verrouille le CONTRAT : les deux adaptateurs
  (wrapper legacy + faux in-memory) satisfont le port (instance + signatures) ;
- en plus de la conformité, vérifie le comportement attendu du port
  (isolation d'erreurs, one-shot, émission) pour les deux implémentations.
"""

from __future__ import annotations

import asyncio
import inspect

from app.domain.ports import EventBusPort
from app.infrastructure.events import InMemoryEventBus
from app.infrastructure.events.legacy_event_bus import LegacyEventBus

METHODS = ("on", "once", "off", "emit", "emit_async", "clear", "listener_count")


def _buses():
    """Instancie les deux implémentations conformes."""
    return [InMemoryEventBus(), LegacyEventBus()]


# --- Conformité au port ------------------------------------------------------


def test_impls_are_event_bus_port():
    for bus in _buses():
        assert isinstance(bus, EventBusPort), type(bus).__name__


def test_impls_implement_full_port_signature():
    for bus in _buses():
        for name in METHODS:
            port_params = set(inspect.signature(getattr(EventBusPort, name)).parameters)
            impl_params = set(inspect.signature(getattr(type(bus), name)).parameters)
            assert port_params <= impl_params, (
                f"{type(bus).__name__}.{name} : {port_params - impl_params}"
            )


# --- Comportement : émission / one-shot / isolation --------------------------


def _capture(sink: list[dict]):
    """Handler lié à *sink* — évite B023 (closure sur variable de boucle)."""
    def _h(**kw):
        sink.append(kw)
    return _h


def _count(sink: list[int]):
    """Handler qui incrémente *sink* — liaison explicite pour rester B023-safe."""
    def _h(**kw):
        sink.append(1)
    return _h


def test_on_emit_receives_event_type_and_kwargs():
    for bus in _buses():
        received: list[dict] = []
        bus.on("agent.tool_start", _capture(received))
        bus.emit("agent.tool_start", tool="web_search", run_id="r1")
        assert len(received) == 1
        assert received[0]["tool"] == "web_search"
        assert received[0]["run_id"] == "r1"
        assert received[0]["event_type"] == "agent.tool_start"
        bus.clear()


def test_once_excutes_then_deregisters():
    for bus in _buses():
        calls: list[int] = []
        bus.once("agent.notif", _count(calls))
        bus.emit("agent.notif")
        bus.emit("agent.notif")
        assert len(calls) == 1, type(bus).__name__
        bus.clear()


def test_error_isolation_never_propagates():
    for bus in _buses():

        def boom(**kw):
            raise RuntimeError("boom")

        bus.on("agent.bad", boom)
        # Ne doit pas lever (l'isolation est une responsabilité de l'impl).
        bus.emit("agent.bad", x=1)
        bus.clear()


def test_listener_count_and_off():
    for bus in _buses():
        h = lambda **kw: None  # noqa: E731
        bus.on("agent.evt", h)
        assert bus.listener_count("agent.evt") == 1
        bus.off("agent.evt", h)
        assert bus.listener_count("agent.evt") == 0
        bus.clear()


# --- Fantômas : in-memory historise, legacy délègue --------------------------


def test_in_memory_records_history():
    bus = InMemoryEventBus()
    bus.emit("agent.approval_pending", request_id="req-1")
    bus.emit("agent.run_finished", run_id="r1")
    assert [e["event_type"] for e in bus.history] == [
        "agent.approval_pending",
        "agent.run_finished",
    ]


def _record_into(sink: list[str], value: str):
    """Handler lié (value) — évite B023 dans une boucle."""
    return lambda **kw: sink.append(value)


def test_emit_async_gathers_handlers():
    async def scenario():
        events: list[str] = []
        for bus in _buses():
            bus.on("agent.evt", _record_into(events, "x"))
            await bus.emit_async("agent.evt", v="x")
            bus.clear()
        return events

    assert asyncio.run(scenario()) == ["x", "x"]
