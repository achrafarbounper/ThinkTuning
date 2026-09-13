"""Systeme d'evenements pub/sub thread-safe pour l'agent ThinkTuning."""

import asyncio
import logging
import threading
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("thinktuning.agent.events")


class EventBus:
    """Bus d'evenements pub/sub avec isolation d'erreurs."""

    def __init__(self) -> None:
        self._handlers: Dict[str, List[Callable]] = defaultdict(list)
        self._lock = threading.RLock()
        self._once_handlers: set[int] = set()

    def on(self, event_type: str, handler: Callable) -> None:
        """Enregistre un handler pour un type d'evenement."""
        with self._lock:
            self._handlers[event_type].append(handler)
            logger.debug("event_bus: handler registered for '%s'", event_type)

    def once(self, event_type: str, handler: Callable) -> None:
        """Enregistre un handler one-shot."""
        with self._lock:
            self._handlers[event_type].append(handler)
            self._once_handlers.add(id(handler))

    def off(self, event_type: str, handler: Callable) -> None:
        """Supprime un handler."""
        with self._lock:
            if event_type in self._handlers:
                try:
                    self._handlers[event_type].remove(handler)
                    self._once_handlers.discard(id(handler))
                except ValueError:
                    pass

    def emit(self, event_type: str, **kwargs: Any) -> None:
        """Emet un evenement de maniere synchrone."""
        with self._lock:
            handlers = list(self._handlers.get(event_type, []))

        timestamp = datetime.now(timezone.utc).isoformat()
        event_data = {"event_type": event_type, "timestamp": timestamp, **kwargs}
        to_remove: List[int] = []

        for handler in handlers:
            try:
                handler(**event_data)
            except Exception as exc:
                logger.warning("event_bus: handler error for '%s': %s", event_type, exc)
            if id(handler) in self._once_handlers:
                to_remove.append(id(handler))

        if to_remove:
            with self._lock:
                for hid in to_remove:
                    self._once_handlers.discard(hid)
                    self._handlers[event_type] = [
                        h for h in self._handlers.get(event_type, []) if id(h) != hid
                    ]

    def clear(self, event_type: Optional[str] = None) -> None:
        """Supprime tous les handlers (ou seulement ceux d'un type)."""
        with self._lock:
            if event_type is None:
                self._handlers.clear()
                self._once_handlers.clear()
            else:
                self._handlers.pop(event_type, None)

    def listener_count(self, event_type: str) -> int:
        """Nombre de handlers enregistres pour un evenement."""
        with self._lock:
            return len(self._handlers.get(event_type, []))

    async def emit_async(self, event_type: str, **kwargs: Any) -> None:
        """Emet un evenement de maniere asynchrone."""
        with self._lock:
            handlers = list(self._handlers.get(event_type, []))

        timestamp = datetime.now(timezone.utc).isoformat()
        event_data = {"event_type": event_type, "timestamp": timestamp, **kwargs}
        async_handlers: List[Callable] = []
        sync_handlers: List[Callable] = []

        for h in handlers:
            if asyncio.iscoroutinefunction(h):
                async_handlers.append(h)
            else:
                sync_handlers.append(h)

        if async_handlers:
            results = await asyncio.gather(
                *(h(**event_data) for h in async_handlers),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    logger.warning(
                        "event_bus: async handler error '%s': %s", event_type, result
                    )

        for handler in sync_handlers:
            try:
                handler(**event_data)
            except Exception as exc:
                logger.warning(
                    "event_bus: sync handler error '%s': %s", event_type, exc
                )


# ============================================================
# Singleton global
# ============================================================

_default_bus: Optional[EventBus] = None
_bus_lock = threading.Lock()


def get_event_bus() -> EventBus:
    """Retourne l'instance singleton de l'EventBus."""
    global _default_bus
    if _default_bus is None:
        with _bus_lock:
            if _default_bus is None:
                _default_bus = EventBus()
    return _default_bus


def reset_event_bus() -> None:
    """Reinitialise le singleton (pour les tests)."""
    global _default_bus
    with _bus_lock:
        _default_bus = None


def on(event_type: str, handler: Callable) -> None:
    """Enregistre un handler sur le bus global."""
    get_event_bus().on(event_type, handler)


def once(event_type: str, handler: Callable) -> None:
    """Enregistre un handler one-shot sur le bus global."""
    get_event_bus().once(event_type, handler)


def off(event_type: str, handler: Callable) -> None:
    """Supprime un handler du bus global."""
    get_event_bus().off(event_type, handler)


def emit(event_type: str, **kwargs: Any) -> None:
    """Emet un evenement sur le bus global."""
    get_event_bus().emit(event_type, **kwargs)


def listener_count(event_type: str) -> int:
    """Nombre de handlers sur le bus global."""
    return get_event_bus().listener_count(event_type)


@contextmanager
def temporary_listener(event_type: str, handler: Callable):
    """Context manager pour un listener temporaire."""
    on(event_type, handler)
    try:
        yield
    finally:
        off(event_type, handler)