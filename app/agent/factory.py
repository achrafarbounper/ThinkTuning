"""Factory du noyau agentique : câblage des implémentations réelles.

Composition root légère : assemble ``AgentCore`` avec le client LLM choisi par
``AGENT_LLM_V2`` (``HttpLLMClient`` par défaut depuis la bascule v2 en
production ; client legacy ``ia/agent/llm_client.py`` en repli via
``AGENT_LLM_V2=0``) et l'adaptateur du registre d'outils, en lisant les
réglages depuis ``app/config/settings.py``.

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


def llm_endpoint(settings):
    """Résout (url, api_key) selon le provider et les Settings centralisés.

    Extrait du code du client legacy pour être réutilisé par le client v2
    (``app/infrastructure/llm``) : source unique de la sélection d'endpoint.
    """
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
    return url, api_key


def build_legacy_llm_client():
    """Construit le client LLM legacy avec les réglages centralisés.

    Retourne l'instance ``ia.agent.llm_client.LLMClient``. L'import passe par
    l'identité de PAQUET réel (``ia.agent``) — jamais par l'identité nue
    ``agent`` qui n'existe que via un hack ``sys.path``
    (cf. tests/test_sys_path_guard.py)."""
    settings = get_settings()
    from ia.agent import llm_client as _llm_mod

    url, api_key = llm_endpoint(settings)

    return _llm_mod.LLMClient(
        url=url,
        model=settings.agent_model_name,
        timeout=settings.agent_timeout_seconds,
        context_length=settings.agent_context_length,
        provider=settings.agent_provider.value,
        api_key=api_key,
    )


def llm_v2_enabled() -> bool:
    """Vrai si le client LLM v2 est actif (AGENT_LLM_V2 — activé par défaut).

    Même convention que ``new_core_enabled()`` : l'environnement (comptabilité
    ``monkeypatch.setenv``) est lu en priorité, puis ``Settings.flag_llm_v2``.
    Repli ``True`` si les Settings ne sont pas chargeables : depuis la bascule
    en production, v2 est le comportement par défaut (``AGENT_LLM_V2=0`` pour
    forcer le repli legacy).
    """
    env = os.getenv("AGENT_LLM_V2")
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return get_settings().flag_llm_v2
    except Exception:
        return True


def build_llm_client():
    """Seam du client LLM : choisit l'implémentation selon ``AGENT_LLM_V2``.

    - défaut (flag absent ou ``1``) → ``HttpLLMClient`` (implémentation propre
      httpx du port ``LLMClientPort``, cf. ``app/infrastructure/llm``) ;
    - ``AGENT_LLM_V2=0`` → client legacy (repli, tant que le chemin v1 vit).
    """
    if llm_v2_enabled():
        from app.infrastructure.llm.http_client import HttpLLMClient

        settings = get_settings()
        url, api_key = llm_endpoint(settings)
        return HttpLLMClient(
            url=url,
            model=settings.agent_model_name,
            provider=settings.agent_provider.value,
            api_key=api_key,
            timeout=settings.agent_timeout_seconds,
            context_length=settings.agent_context_length,
        )
    return build_legacy_llm_client()


def build_agent_core(approval_gateway=None, on_tool_event=None,
                     enable_thinking=False, on_thinking=None,
                     event_bus=None,
                     intent_classifier=None) -> AgentCore:
    """Assemble le noyau agentique complet (LLM réel + registre legacy).

    ``approval_gateway`` : callback optionnel ``(Action) -> bool`` injecté au
    noyau (le run /ask/core passe la gateway de reprise par empreinte).
    ``on_tool_event`` : callback optionnel ``(dict) -> None`` recevant les
    événements d'outils en temps réel (SSE /ask/core/stream).
    ``enable_thinking`` : active le mode « Réflexion » du noyau (chaque round
    du LLM diffuse son raisonnement via call_stream).
    ``on_thinking`` : callback optionnel ``(str) -> None`` diffusant chaque
    fragment de raisonnement en temps réel (SSE thinking_delta).
    ``event_bus`` : ``EventBusPort`` optionnel sur lequel le noyau publie les
    événements de cycle de vie (run_start, tool_start/tool_end, thinking,
    approval_pending, run_finished). Un bus PAR RUN (``InMemoryEventBus``)
    évite tout cross-talk entre flux concurrents.
    ``intent_classifier`` : classifieur d'intention optionnel (chat/action,
    Phase 4). Reste observatoire : détermine ``AgentCore.last_intent`` et
    émet ``agent.intent_detected``, sans modifier la boucle LLM.
    """
    settings = get_settings()
    registry = LegacyToolRegistryAdapter()
    llm = build_llm_client()
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
        event_bus=event_bus,
        max_rounds=settings.agent_max_llm_rounds,
        max_tool_calls=settings.agent_max_tool_calls,
        intent_classifier=intent_classifier,
    )


def new_core_enabled() -> bool:
    """Vrai si le noyau agentique v2 est actif (AGENT_NEW_CORE — défaut : activé).

    Source de vérité : ``Settings.flag_new_core`` (convention des autres
    flags), alimentée par ``AGENT_NEW_CORE`` — y compris via le fichier
    ``.env`` que ``os.getenv`` ne voit pas. L'environnement est lu en
    priorité pour rester compatible avec ``monkeypatch.setenv`` sans
    ``get_settings.cache_clear()`` (convention des tests existants).
    Repli ``True`` si les Settings ne sont pas chargeables : depuis la
    bascule en production, le noyau v2 est le comportement par défaut
    (``AGENT_NEW_CORE=0`` pour forcer le repli sur les routes v1 restantes).
    """
    env = os.getenv("AGENT_NEW_CORE")
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return get_settings().flag_new_core
    except Exception:
        return True
