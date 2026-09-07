"""Implémentations ``EventBusPort`` (bus d'événements pub/sub).

- ``default_event_bus()`` : renvoie le bus par défaut (singleton legacy) ;
- ``LegacyEventBus``    : wrapper strangler vers ``ia/agent/event_bus`` — rien
  n'est réécrit, le bus singleton existant est exposé derrière le port ;
- ``InMemoryEventBus``  : faux déterministe (aucune dépendance legacy) pour les
  tests de use-cases et la future implémentation in-process (asyncio).

Objectif : permettre aux use-cases / adaptateurs SSE-WS d'émettre des
événements via ``EventBusPort`` sans connaître le bus legacy concret.
"""

from app.infrastructure.events.in_memory import InMemoryEventBus
from app.infrastructure.events.legacy_event_bus import LegacyEventBus


def default_event_bus():
    """Returns the default bus: the legacy singleton wrapped as ``EventBusPort``."""
    from app.infrastructure.events.legacy_event_bus import legacy_bus

    return legacy_bus()


__all__ = ["default_event_bus", "LegacyEventBus", "InMemoryEventBus"]
