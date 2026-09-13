"""Explication LLM OpenRouter — use-case noyau v2 (remplace le runner v1).

Historique du couplage retiré : la route ``POST /explain`` passait par la
façade strangler ``app.application.agent_cache.ask_agent_openrouter``, qui
assemblait un ``AgentRunner``/``AgentCore`` du runtime v1
(``app/agent/legacy/``) forcé sur le provider OpenRouter.

Ce use-case fournit le même service SANS le runtime v1 :
    - configuration typée ``AgentConfig`` (``app.agent.settings``), avec le
      provider forcé sur ``openrouter`` indépendamment de la config par défaut ;
    - client LLM v2 ``HttpLLMClient`` (``app.infrastructure.llm``) — aucun
      import legacy ;
    - erreurs : ``ValueError`` si la clé OpenRouter manque, exceptions ``httpx``
      relayées telles quelles (la route les traduit en 500/504/502).

Contrat de sortie : la réponse texte du LLM (``str``).
"""

from __future__ import annotations

from app.agent.settings import (
    DEFAULT_OPENROUTER_MODEL_NAME,
    AgentProvider,
    get_agent_config,
)
from app.infrastructure.llm.http_client import HttpLLMClient

__all__ = ["ask_agent_openrouter", "DEFAULT_OPENROUTER_MODEL_NAME"]


def ask_agent_openrouter(prompt: str, model: str | None = None) -> str:
    """Envoie le prompt à l'agent via un client FORCÉ sur le provider OpenRouter.

    ``prompt`` : instruction d'explication construite par la route (la
    prédiction DistilBERT sert de contexte).
    ``model`` : ID OpenRouter explicite (ex. « vendor/model ») ; absent ou
    vide, ``DEFAULT_OPENROUTER_MODEL_NAME`` est utilisé.

    Lève :
        - ``ValueError`` : aucune clé OpenRouter dans la config effective
          (le provider forcé invalide la configuration) ;
        - exceptions ``httpx`` (timeout / connexion / HTTP) : relayées à la
          route qui choisit la réponse HTTP (504 / 502).
    """
    effective_model = (model or "").strip() or DEFAULT_OPENROUTER_MODEL_NAME
    try:
        # Fail-fast pydantic : provider=openrouter sans clé -> ValueError.
        openrouter_cfg = get_agent_config().model_copy(
            update={"provider": AgentProvider.OPENROUTER}
        )
    except ValueError as exc:
        raise ValueError(
            "Provider LLM « openrouter » sélectionné mais aucune clé API n'est "
            "définie. Renseignez OPENROUTER_API_KEY (ou enregistrez-la dans la "
            "page Paramètres)."
        ) from exc

    url, api_key = openrouter_cfg.endpoint()
    llm = HttpLLMClient(
        url=url,
        model=effective_model,
        provider="openrouter",
        api_key=api_key,
        timeout=openrouter_cfg.timeout_seconds,
        context_length=openrouter_cfg.context_length,
        temperature=openrouter_cfg.temperature,
    )
    return llm.call([{"role": "user", "content": prompt}])
