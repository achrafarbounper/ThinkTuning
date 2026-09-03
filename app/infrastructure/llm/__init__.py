"""Implémentations ``LLMClientPort`` (client LLM).

- ``default_llm_client()`` : choisit l'implémentation selon le flag
  ``AGENT_LLM_V2`` (le client legacy par défaut, ou l'implémentation v2).
- ``StubLLMClient`` : implémentation v2 déterministe (zéro réseau), utile
  pour les tests de use-cases hors ligne et comme squelette du futur client
  HTTP « propre » (le remplacement de ``ia/agent/llm_client.py`` sans changer
  les use-cases, cf. Phase 3 de la migration).

Le vrai client HTTP propre (httpx + retry + circuit breaker) remplacera le
stub derrière le même port dans un incrément ultérieur, sans toucher au
domaine.
"""

from __future__ import annotations

from app.domain.ports import LLMClientPort
from app.infrastructure.llm.stub_client import StubLLMClient

__all__ = ["LLMClientPort", "StubLLMClient", "default_llm_client"]


def default_llm_client(llm_v2: bool = False, **overrides: object) -> LLMClientPort:
    """Implémentation LLM par défaut.

    ``llm_v2=True`` → ``StubLLMClient`` (option v2, déterministe, hors réseau) ;
    ``llm_v2=False`` → client legacy (assemblé par la factory, import paquet
    réel ``ia.agent`` — jamais l'identité nue).
    """
    if llm_v2:
        return StubLLMClient(
            response=str(overrides.get("response", "réponse de stub")),
        )
    from app.agent.factory import build_legacy_llm_client

    return build_legacy_llm_client()


# Conformité structurelle explicite.
_REF: LLMClientPort = StubLLMClient
