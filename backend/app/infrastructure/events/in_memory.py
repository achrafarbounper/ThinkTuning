"""Faux déterministe ``EventBusPort`` (in-process, sans dépendance legacy).

Implémente le port pub/sub avec isolation d'erreurs : ``emit`` ne propage
jamais une exception de handler ; ``emit_async`` étant un simple wrapr sur le
dispatch synchrone pour ce fake.

Comporte un ``history`` (liste des événements émis) — utile pour les tests de
use-cases qui vérifient qu'un événement a bien été publié.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from app.domain.ports import EventBusPort


class InMemoryEventBus(EventBusPort):
    """Bus pub/sub en mémoire, conforme ``EventBusPort``."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable[..., Any]]] = {}
        self._once: set[int] = set()
        self.history: list[dict[str, Any]] = []

    def on(self, event_type: str, handler: Callable[..., Any]) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    def once(self, event_type: str, handler: Callable[..., Any]) -> None:
        self._handlers.setdefault(event_type, []).append(handler)
        self._once.add(id(handler))

    def off(self, event_type: str, handler: Callable[..., Any]) -> None:
        if event_type in self._handlers:
            try:
                self._handlers[event_type].remove(handler)
                self._once.discard(id(handler))
            except ValueError:
                pass

    def emit(self, event_type: str, **kwargs: Any) -> None:
        event = {"event_type": event_type, **kwargs}
        self.history.append(event)
        handlers = list(self._handlers.get(event_type, []))
        for handler in handlers:
            try:
                handler(**event)
            except Exception:  # noqa: BLE001 - isolation d'erreurs du port
                continue
            if id(handler) in self._once:
                self.off(event_type, handler)

    async def emit_async(self, event_type: str, **kwargs: Any) -> None:
        await asyncio.to_thread(self.emit, event_type, **kwargs)

    def clear(self, event_type: str | None = None) -> None:
        if event_type is None:
            self._handlers.clear()
            self._once.clear()
        else:
            self._handlers.pop(event_type, None)

    def listener_count(self, event_type: str) -> int:
        return len(self._handlers.get(event_type, []))


# Conformité structurelle explicite (signatures vérifiées par test).
_REF: EventBusPort = InMemoryEventBus
