# project/app/infrastructure/mcp/sampling/sampling_adapter.py
"""Adaptateur Sampling MCP — ``SamplingPort`` via ``LLMClientPort`` (tache 15).

Delegation stricte (zero regle dupliquée, cf. legacy_tool_provider) :

    SamplingRequest (validee) -> effective_messages()
      -> ``LLMClientPort.call(messages)`` -> ``SamplingResponse``

- ``create_message(request)`` : contrat CANONIQUE ; valide que la reponse
  LLM est non vide (un LLM muet = ``LLMClientError``, jamais une reponse
  vide silencieuse), erreurs provider enveloppees en ``LLMClientError``
  (domaine) sauf si deja typées ;
- ``create_message(messages, max_tokens=...)`` : forme LEGACY (tache 15 —
  ``create_message(messages, max_tokens)``) supportée par normalisation
  d'arguments (liste brute ou ``SamplingRequest``) ;
- ``create_text(...)`` : commodite S1 deleguant a ``create_message`` ;
- temperature / max_tokens : PREFERENCES transportées dans la requete —
  ``HttpLLMClient`` fige ces reglages a la construction (pas de kwargs
  runtime) : l'adaptateur les CONSERVE (traçabilite) sans les appliquer ;
- ``model`` de la reponse : ``getattr(llm, "model", "")`` (``HttpLLMClient``
  l'expose ; fakes -> "") ;
- thread-safe / stateless (le client LLM porte le retry + circuit breaker).
"""

from __future__ import annotations

import logging
from typing import Any

from app.domain.entities.mcp import SamplingRequest, SamplingResponse
from app.domain.errors import LLMClientError
from app.domain.ports.mcp_ports import SamplingPort, _sampling_create_text
from app.domain.ports.ports import LLMClientPort, Message

logger = logging.getLogger("thinktuning.mcp.sampling")

__all__ = ["SamplingAdapter", "build_sampling_adapter"]


class SamplingAdapter(SamplingPort):
    """``SamplingPort`` adossé a un ``LLMClientPort`` existant."""

    def __init__(self, llm: LLMClientPort) -> None:
        self._llm = llm

    @property
    def llm(self) -> LLMClientPort:
        """Client LLM sous-jacent (introspection tests / composition)."""
        return self._llm

    def create_message(
        self,
        request: SamplingRequest | list[Message],
        max_tokens: int | None = None,
    ) -> SamplingResponse:
        """Genere une completion (forme canonique OU legacy).

        Args:
            request: ``SamplingRequest`` validee OU liste brute de messages
                (forme legacy ``create_message(messages, max_tokens)`` —
                normalisee en ``SamplingRequest`` : validation Pydantic
                fail-fast) ;
            max_tokens: budget legacy (ignore si ``request`` est deja une
                ``SamplingRequest`` avec son propre ``max_tokens``).

        Raises:
            LLMClientError: echec provider OU completion vide.
        """
        if isinstance(request, SamplingRequest):
            sampling_request = request
        else:
            sampling_request = SamplingRequest(
                messages=[dict(m) for m in request],
                max_tokens=max_tokens,
            )
        messages = sampling_request.effective_messages()
        try:
            text = self._llm.call(messages)
        except LLMClientError:
            raise
        except Exception as exc:
            raise LLMClientError(f"Echec sampling LLM : {exc}") from exc
        if not isinstance(text, str) or not text.strip():
            raise LLMClientError("Echec sampling LLM : reponse vide du provider.")
        model = str(getattr(self._llm, "model", "") or "")
        logger.debug("MCP sampling OK (%d chars, model=%s)", len(text), model or "?")
        return SamplingResponse(text=text, model=model)

    def create_text(
        self,
        messages: list[Message],
        *,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> str:
        """Texte brut via ``create_message`` (compatibilite S1)."""
        return _sampling_create_text(
            self,
            messages,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            temperature=temperature,
        )


def build_sampling_adapter(llm: LLMClientPort | None = None) -> SamplingAdapter:
    """Fabrique : ``SamplingAdapter`` sur ``llm`` (defaut = client standard).

    ``llm=None`` -> ``default_llm_client()`` (``HttpLLMClient`` configure
    depuis ``Settings``) ; les tests injectent ``StubLLMClient`` ou un fake.
    """
    if llm is None:
        from app.infrastructure.llm import default_llm_client

        llm = default_llm_client()
    return SamplingAdapter(llm)
