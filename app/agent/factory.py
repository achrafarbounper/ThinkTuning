"""Factory du noyau agentique : câblage des implémentations réelles.

Composition root légère : assemble ``AgentCore`` avec le client LLM legacy
(``ia/agent/llm_client.py``, qui porte déjà retry + circuit breaker + streaming)
et l'adaptateur du registre d'outils, en lisant les réglages depuis
``app/config/settings.py``.

Bascule par feature flag : ``AGENT_NEW_CORE=1`` active le nouveau noyau.
Tant que le flag est absent, le comportement historique d'
``api/routes/agent.py`` est strictement préservé (convention du programme
d'enhancement : rollout incrémental, flags désactivés par défaut).
"""

from __future__ import annotations

import logging
import os

from app.agent.core import AgentCore
from app.config.settings import AgentProvider, get_settings
from app.infrastructure.legacy_registry import LegacyToolRegistryAdapter

logger = logging.getLogger("thinktuning.agent.factory")


def build_legacy_llm_client():
    """Construit le client LLM legacy avec les réglages centralisés.

    Retourne l'instance ``ia.agent.llm_client.LLMClient`` (double identité
    d'import gérée comme pour le registre)."""
    settings = get_settings()
    try:
        from ia.agent import llm_client as _llm_mod
    except ImportError:
        from agent import llm_client as _llm_mod  # type: ignore[no-redef]

    url = {
        AgentProvider.OLLAMA: settings.agent_ollama_url,
        AgentProvider.OPENROUTER: settings.agent_openrouter_url,
        AgentProvider.HF: settings.agent_ollama_url,  # endpoint HF : réutilise ollama_url
    }[settings.agent_provider]

    api_key = None
    if settings.agent_provider is AgentProvider.OPENROUTER:
        api_key = settings.openrouter_api_key
    elif settings.agent_provider is AgentProvider.HF:
        api_key = settings.effective_hf_key

    return _llm_mod.LLMClient(
        url=url,
        model=settings.agent_model_name,
        timeout=settings.agent_timeout_seconds,
        context_length=settings.agent_context_length,
        provider=settings.agent_provider.value,
        api_key=api_key,
    )


def build_agent_core(approval_gateway=None, on_tool_event=None,
                     enable_thinking=False, on_thinking=None) -> AgentCore:
    """Assemble le noyau agentique complet (LLM réel + registre legacy).

    ``approval_gateway`` : callback optionnel ``(Action) -> bool`` injecté au
    noyau (le run /ask/core passe la gateway de reprise par empreinte).
    ``on_tool_event`` : callback optionnel ``(dict) -> None`` recevant les
    événements d'outils en temps réel (SSE /ask/core/stream).
    ``enable_thinking`` : active le mode « Réflexion » du noyau (chaque round
    du LLM diffuse son raisonnement via call_stream).
    ``on_thinking`` : callback optionnel ``(str) -> None`` diffusant chaque
    fragment de raisonnement en temps réel (SSE thinking_delta)."""
    settings = get_settings()
    registry = LegacyToolRegistryAdapter()
    llm = build_legacy_llm_client()
    logger.info(
        "Noyau agentique assemblé : provider=%s model=%s outils=%d flags=%s",
        settings.agent_provider.value, settings.agent_model_name,
        len(registry.tool_names()), settings.active_flags(),
    )
    return AgentCore(
        llm, registry,
        approval_gateway=approval_gateway,
        on_tool_event=on_tool_event,
        enable_thinking=enable_thinking,
        on_thinking=on_thinking,
        max_rounds=settings.agent_max_llm_rounds,
        max_tool_calls=settings.agent_max_tool_calls,
    )


def new_core_enabled() -> bool:
    """Vrai si la bascule du noyau agentique v2 est activée.

    Source de vérité : ``Settings.flag_new_core`` (convention des autres
    flags), alimentée par ``AGENT_NEW_CORE`` — y compris via le fichier
    ``.env`` que ``os.getenv`` ne voit pas. L'environnement est lu en
    priorité pour rester compatible avec ``monkeypatch.setenv`` sans
    ``get_settings.cache_clear()`` (convention des tests existants).
    """
    env = os.getenv("AGENT_NEW_CORE")
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return get_settings().flag_new_core
    except Exception:
        # Settings non chargeables (env incomplet) : défaut sûr du rollout
        # incrémental — le noyau v2 reste désactivé.
        return False
