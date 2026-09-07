"""Wrapper strangler du bus d'événements legacy derrière ``EventBusPort``.

Délègue au singleton ``ia.agent.event_bus`` — la mémoire du bus et son
isolation d'erreurs restent celles du legacy, qu'on n'a pas besoin de
réécrire : ``emit``/``emit_async`` ne remontent jamais d'exception (isolées
dans le legacy).

Le portage complet (implémentation in-process propre) est documenté comme
remplacement éventuel de ce wrapper (Phase 3), sans changer le port.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.domain.ports import EventBusPort
from ia.agent.event_bus import get_event_bus


class LegacyEventBus(EventBusPort):
    """Adaptateur ``EventBusPort`` vers le singleton legacy."""

    def __init__(self, bus=None) -> None:
        # bus optionnel pour les tests (par défaut : singleton legacy).
        self._bus = bus if bus is not None else get_event_bus()

    def on(self, event_type: str, handler: Callable[..., Any]) -> None:
        self._bus.on(event_type, handler)

    def once(self, event_type: str, handler: Callable[..., Any]) -> None:
        self._bus.once(event_type, handler)

    def off(self, event_type: str, handler: Callable[..., Any]) -> None:
        self._bus.off(event_type, handler)

    def emit(self, event_type: str, **kwargs: Any) -> None:
        self._bus.emit(event_type, **kwargs)

    def emit_async(self, event_type: str, **kwargs: Any) -> Any:
        return self._bus.emit_async(event_type, **kwargs)

    def clear(self, event_type: str | None = None) -> None:
        self._bus.clear(event_type)

    def listener_count(self, event_type: str) -> int:
        return self._bus.listener_count(event_type)


def legacy_bus() -> LegacyEventBus:
    """Wrapper autour du singleton legacy (point d'injection par défaut)."""
    return LegacyEventBus()


# Conformité structurelle explicite (signatures vérifiées par test).
_REF: EventBusPort = LegacyEventBus
