"""Factory du noyau agentique : câblage des implémentations réelles.

Composition root légère : assemble ``AgentCore`` avec le client LLM choisi par
le flag ``AGENT_LLM_V2`` (``HttpLLMClient`` par défaut depuis la bascule v2 en
production ; client legacy ``ia/agent/llm_client.py`` en repli via
``AGENT_LLM_V2=0``) et l'adaptateur du registre d'outils. La configuration
effective (provider, modèle, URLs, clés API, timeout/contexte, budgets, flags)
vient du module de configuration de l'IHM (``app.agent.settings.get_agent_config``
— store persistant MongoDB, cf. SCRUM-138) : ``app/config/settings.py`` ne porte
plus AUCUNE configuration d'agent.

Bascule par feature flag : ``AGENT_NEW_CORE=1`` active le nouveau noyau.
Tant que le flag est absent, le comportement historique d'
``api/routes/agent.py`` est strictement préservé (convention du programme
d'enhancement : rollout incrémental, flags désactivés par défaut).
"""

from __future__ import annotations

import logging
import os

from app.agent.core import AgentCore
from app.agent.settings import AgentConfig, get_agent_config
from app.infrastructure.legacy_registry import LegacyToolRegistryAdapter

logger = logging.getLogger("thinktuning.agent.factory")


def llm_endpoint(config: AgentConfig | None = None):
    """Résout (url, api_key) selon le provider de la configuration effective.

    ``config`` : ``AgentConfig`` injectable (tests) ; absent, la configuration
    est chargée depuis le module de configuration de l'IHM (base MongoDB).

    Extrait du code du client legacy pour être réutilisé par le client v2
    (``app/infrastructure/llm``) : source unique de la sélection d'endpoint.
    """
    return (config or get_agent_config()).endpoint()


def build_legacy_llm_client(model: str | None = None, *, think: bool = False):
    """Construit le client LLM legacy avec la configuration de l'IHM.

    ``model`` : surcharge ponctuelle du modèle demandé par le client
    (sélecteur du chat) ; absent/vide : modèle de la configuration effective
    (base MongoDB du module IHM).
    ``think`` : active la réflexion native du provider (Ollama ``think`` —
    sans effet sur les autres providers).

    Retourne l'instance ``ia.agent.llm_client.LLMClient``. L'import passe par
    l'identité de PAQUET réel (``ia.agent``) — jamais par l'identité nue
    ``agent`` qui n'existe que via un hack ``sys.path``
    (cf. tests/test_sys_path_guard.py)."""
    config = get_agent_config()
    from ia.agent import llm_client as _llm_mod

    url, api_key = config.endpoint()

    return _llm_mod.LLMClient(
        url=url,
        model=model or config.model_name,
        timeout=config.timeout_seconds,
        context_length=config.context_length,
        provider=str(config.provider),
        api_key=api_key,
        think=think,
    )


def llm_v2_enabled() -> bool:
    """Vrai si le client LLM v2 est actif (AGENT_LLM_V2 — activé par défaut).

    Même convention que ``new_core_enabled()`` : l'environnement (comptabilité
    ``monkeypatch.setenv``) est lu en priorité, puis le flag persisté du module
    de configuration de l'IHM (``AgentConfig.flag_llm_v2``, base MongoDB).
    Repli ``True`` si la configuration n'est pas chargeable : depuis la bascule
    en production, v2 est le comportement par défaut (``AGENT_LLM_V2=0`` pour
    forcer le repli legacy).
    """
    env = os.getenv("AGENT_LLM_V2")
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return get_agent_config().flag_llm_v2
    except Exception:
        return True


def build_llm_client(model: str | None = None, *, think: bool = False):
    """Seam du client LLM : choisit l'implémentation selon ``AGENT_LLM_V2``.

    - défaut (flag absent ou ``1``) → ``HttpLLMClient`` (implémentation propre
      httpx du port ``LLMClientPort``, cf. ``app/infrastructure/llm``) ;
    - ``AGENT_LLM_V2=0`` → client legacy (repli, tant que le chemin v1 vit).

    ``model`` : surcharge ponctuelle du modèle demandé par le client
    (sélecteur du chat) ; absent/vide : modèle de la configuration effective
    du module IHM (base MongoDB).
    ``think`` : active la réflexion native du provider (Ollama ``think`` —
    sans effet sur les autres providers).
    """
    if llm_v2_enabled():
        from app.infrastructure.llm.http_client import HttpLLMClient

        config = get_agent_config()
        url, api_key = config.endpoint()
        return HttpLLMClient(
            url=url,
            model=model or config.model_name,
            provider=str(config.provider),
            api_key=api_key,
            timeout=config.timeout_seconds,
            context_length=config.context_length,
            think=think,
        )
    return build_legacy_llm_client(model=model, think=think)


def build_agent_core(
    approval_gateway=None,
    on_tool_event=None,
    enable_thinking=False,
    on_thinking=None,
    event_bus=None,
    intent_classifier=None,
    model=None,
) -> AgentCore:
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
    ``model`` : surcharge ponctuelle du modèle LLM demandé par le client
    (sélecteur du chat) ; absent/vide : modèle des Settings centralisés.
    """
    config = get_agent_config()
    registry = LegacyToolRegistryAdapter()
    llm = build_llm_client(model=model, think=enable_thinking)
    logger.info(
        "Noyau agentique assemblé : provider=%s model=%s outils=%d flags=%s",
        str(config.provider),
        model or config.model_name,
        len(registry.tool_names()),
        config.active_flags(),
    )
    return AgentCore(
        llm,
        registry,
        approval_gateway=approval_gateway,
        on_tool_event=on_tool_event,
        enable_thinking=enable_thinking,
        on_thinking=on_thinking,
        event_bus=event_bus,
        max_rounds=config.max_llm_rounds,
        max_tool_calls=config.max_tool_calls,
        intent_classifier=intent_classifier,
    )


def new_core_enabled() -> bool:
    """Vrai si le noyau agentique v2 est actif (AGENT_NEW_CORE — défaut : activé).

    Source de vérité : le flag persisté du module de configuration de l'IHM
    (``AgentConfig.flag_new_core``, base MongoDB) ; l'environnement
    ``AGENT_NEW_CORE`` est lu en priorité pour rester compatible avec
    ``monkeypatch.setenv`` (convention des tests existants).
    Repli ``True`` si la configuration n'est pas chargeable : depuis la
    bascule en production, le noyau v2 est le comportement par défaut
    (``AGENT_NEW_CORE=0`` pour forcer le repli sur les routes v1 restantes).
    """
    env = os.getenv("AGENT_NEW_CORE")
    if env is not None:
        return env.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return get_agent_config().flag_new_core
    except Exception:
        return True
