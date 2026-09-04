"""Implémentations ``LLMClientPort`` (client LLM).

- ``default_llm_client()`` : choisit l'implémentation selon le flag
  ``AGENT_LLM_V2`` — ``HttpLLMClient`` par défaut (bascule v2 en production),
  client legacy en repli (``AGENT_LLM_V2=0``) tant que le chemin v1 vit ;
- ``HttpLLMClient``     : implémentation v2 complète (streaming NDJSON/SSE,
  retry + circuit breaker, thinking, réparation d'encodage). ``transport``
  injectable pour des tests hors réseau ;
- ``StubLLMClient``     : faux déterministe (aucune I/O) pour les tests de
  use-cases hors ligne.

Décommission (fin de migration) : la suppression de ``ia/agent/llm_client.py``
est liée au retrait du chemin v1 (``core/agent_cache.py`` l'utilise encore).
"""

from __future__ import annotations

from app.domain.ports import LLMClientPort
from app.infrastructure.llm.http_client import HttpLLMClient
from app.infrastructure.llm.stub_client import StubLLMClient

__all__ = [
    "LLMClientPort",
    "HttpLLMClient",
    "StubLLMClient",
    "default_llm_client",
]


def default_llm_client(llm_v2: bool = True, **overrides: object) -> LLMClientPort:
    """Implémentation LLM par défaut, selon la bascule ``AGENT_LLM_V2``.

    ``llm_v2=True`` (défaut) → ``HttpLLMClient`` configuré depuis ``Settings`` ;
    ``llm_v2=False``         → client legacy (assemblé par la factory).

    ``**overrides`` permet de remplacer les réglages (``model``, ``url``…)
    pour les tests / la composition ciblée.
    """
    if llm_v2:
        return _build_http_client(**overrides)
    from app.agent.factory import build_legacy_llm_client

    return build_legacy_llm_client()


def _build_http_client(**overrides: object) -> HttpLLMClient:
    """Construit ``HttpLLMClient`` depuis les Settings centralisés."""
    from app.agent.factory import llm_endpoint
    from app.config.settings import get_settings

    settings = get_settings()
    url, api_key = llm_endpoint(settings)
    kwargs = {
        "url": url,
        "model": settings.agent_model_name,
        "provider": settings.agent_provider.value,
        "api_key": api_key,
        "timeout": settings.agent_timeout_seconds,
        "context_length": settings.agent_context_length,
    }
    kwargs.update({k: v for k, v in overrides.items() if v is not None})
    return HttpLLMClient(**kwargs)  # type: ignore[arg-type]


# Conformité structurelle (signatures vérifiées par test).
_REF: LLMClientPort = HttpLLMClient
