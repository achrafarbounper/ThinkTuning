"""``StubLLMClient`` — implémentation ``LLMClientPort`` déterministe (zéro réseau).

Fait partie des fakes « contrôlés » : aucune dépendance externe, réponse fixe
(``response``). ``call_stream`` reproduit néanmoins le contrat de flux réel en
émettant un seul événement ``on_content`` avant de retourner la réponse.

Pensé pour :
    - tester les use-cases hors ligne (noyau, orchestration, SSE) sans réseau ;
    - servir de squelette de l'implémentation v2 (le futur client HTTP propre
      adoptera exactement ce contrat, cf. Phase 3).

Ne RSÉ le pas le retry/circuit breaker : c'est de la responsabilité de
l'implémentation de production, pas du domaine.
"""

from __future__ import annotations

from collections.abc import Callable

from app.domain.ports import Message


class StubLLMClient:
    """Adapter ``LLMClientPort`` à réponse fixe et déterministe."""

    def __init__(self, response: str = "réponse de stub") -> None:
        self._response = response

    def call(self, messages: list[Message]) -> str:
        return self._response

    def call_stream(
        self,
        messages: list[Message],
        on_thinking: Callable[[str], None] | None = None,
        on_content: Callable[[str], None] | None = None,
    ) -> str:
        if on_content is not None:
            on_content(self._response)
        return self._response
