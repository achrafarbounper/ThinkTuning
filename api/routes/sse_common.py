# project/api/routes/sse_common.py

"""Utilitaires communs aux flux SSE (StreamingResponse ``text/event-stream``).

Politique unique « un flux ne meurt jamais en silence » partagée par les trois
générateurs SSE du backend (``/api/ai``, ``/api/agent/ask/core/stream``,
``/api/agent/multi/ask/stream``) :

  1. **Heartbeat** : la file d'événements est lue avec un plafond temporel ;
     à expiration, le générateur émet un commentaire SSE ``: ping`` qui
     maintient le socket chaud (proxy Vite, intermédiaires, OS) et détecte
     tôt un client mort. Sans lui, un worker silencieux (appel LLM > 15 s,
     outil lent) laisse une connexion inerte que des intermédiaires peuvent
     réinitialiser — visible côté dashboard comme un ``ECONNRESET`` du proxy
     de dev.

  2. **Terminaison propre** : le générateur appelant doit, même en cas
     d'exception imprévue, émettre son événement d'erreur puis ``[DONE]``.
     Une exception qui s'échappe du générateur tronque l'encodage chunked :
     le client lit alors une connexion coupée (RST) au lieu d'un flux
     correctement terminé.
"""

import asyncio
import queue
from typing import Any

# Intervalle entre deux battements de cœur SSE (commentaire ``: ping``).
SSE_PING_INTERVAL_SECONDS = 15.0


async def wait_event(
    events: queue.Queue,
    max_wait: float = SSE_PING_INTERVAL_SECONDS,
) -> tuple[str, Any] | None:
    """Attend le prochain événement ``(kind, payload)`` avec un plafond de temps.

    Retourne ``None`` si ``max_wait`` s'écoule sans événement : l'appelant émet
    alors un ping (``: ping``) et relance l'attente.

    Implémentation : ``queue.get(block, timeout)`` est exécuté DANS le thread
    (via ``asyncio.to_thread``). Un ``asyncio.wait_for`` (ou ``asyncio.timeout``)
    autour d'un ``get()`` infini abandonnerait la tâche asyncio mais laisserait
    le thread du pool bloqué indéfiniment sur la file — une fuite de thread à
    chaque heartbeat. Avec le timeout porté par la queue elle-même, le thread
    se termine toujours proprement, au plus tard au délai imparti.
    """
    try:
        return await asyncio.to_thread(events.get, True, max_wait)
    except queue.Empty:
        return None
