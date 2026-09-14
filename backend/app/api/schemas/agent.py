# project/app/api/schemas/agent.py
"""DTOs Pydantic de la surface HTTP agent (extraits de ``api/routes/agent.py``).

Les modèles de requête/réponse sont des CONTRATS DE PRÉSENTATION : ils
décrivent la validation d'entrée et la sérialisation de sortie des surfaces
``/api/agent/*`` (legacy) et ``/api/v1/agent/*`` (v1). Ils vivent dans la
couche API (``api/schemas/``) — la logique métier correspondante est dans les
use cases ``app/application/agent_surface.py`` (B-5, ADR-0003).

Les 12 modèles sont déplacés TELS QUELS de ``routes/agent.py`` : aucune
modification de champ, de contrainte ou de description — le contrat OpenAPI
reste byte-identique (tests de contrat inchangés).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "AgentProviderPayload",
    "AgentSettingsUpdate",
    "AskRequest",
    "AskResponse",
    "AskStreamRequest",
    "ConnectivityTestRequest",
    "MultiAskRequest",
    "ToolRunRequest",
]


class AskRequest(BaseModel):
    prompt: str = Field(..., min_length=1, description="Instruction envoyée à l'agent.")
    resume_request_id: str | None = Field(
        None,
        description="Relance une tâche en attente : id donné par une réponse "
        "« awaiting_approval » après validation humaine (approve).",
    )
    session_id: str | None = Field(
        None,
        description="Session de conversation "
        "(app/infrastructure/persistence/session_store) où journaliser "
        "l'échange ; absent : aucune persistance côté serveur.",
    )
    model: str | None = Field(
        None,
        max_length=100,
        description="Modèle LLM ; absent/vide = défaut serveur "
        "(parité AskStreamRequest — le sélecteur du chat était auparavant "
        "ignoré sur ce chemin).",
    )
    enable_thinking: bool = Field(
        False,
        description="Mode « Réflexion » (parité AskStreamRequest).",
    )


class AskStreamRequest(BaseModel):
    """Corps de POST /api/agent/ask/core/stream (mode Agent temps réel).

    Même contrat que ``AskRequest`` avec en plus la sélection du modèle LLM
    et du mode « Réflexion » (sélecteurs de l'en-tête du chat).
    """

    prompt: str = Field(..., min_length=1, description="Instruction envoyée à l'agent.")
    resume_request_id: str | None = Field(None)
    model: str | None = Field(
        None, max_length=100, description="Modèle LLM ; absent/vide = défaut serveur."
    )
    enable_thinking: bool = Field(False, description="Mode « Réflexion ». ")
    session_id: str | None = Field(None, description="Conversation cible (persistance).")


class MultiAskRequest(BaseModel):
    """Corps de l'orchestration multi-agents (superviseur/workers)."""

    prompt: str = Field(..., min_length=1, description="Tâche globale soumise au superviseur.")
    model: str | None = Field(
        None, max_length=100, description="Modèle LLM ; absent/vide = défaut serveur."
    )
    parallel: bool = Field(
        True,
        description="Exécution parallèle des sous-tâches INDÉPENDANTES "
        "(défaut : activé — les dépendances déclarées restent séquentielles).",
    )
    resume_request_id: str | None = Field(
        None,
        description="REPRISE NATIVE multi-agents : relance l'orchestration "
        "interrompue sur une validation humaine. L'action approuvée est "
        "rejouée DANS le même worker (empreinte SHA-256 revérifiée), puis la "
        "synthèse finale intègre l'ensemble des résultats. Ne passe JAMAIS "
        "par le noyau mono-agent.",
    )
    enable_thinking: bool = Field(
        False, description="Mode « Réflexion » des workers (agent.worker.thinking)."
    )
    mode: str = Field(
        "full",
        description="Granularité du streaming SSE : « full » (tous événements) "
        "ou « compact » (plan / worker.* / done — les événements "
        "d'observabilité tool / synthesizing sont filtrés). La réflexion des "
        "workers (agent.worker.thinking) passe dans les deux modes : elle est "
        "requise par l'IHM dès que enable_thinking est actif.",
    )


class AskResponse(BaseModel):
    response: str
    model: str
    status: str = "completed"
    request_id: str | None = None
    approval: dict | None = None


class ToolRunRequest(BaseModel):
    tool: str = Field(..., description="Nom de l'outil (ex: 'add', 'write_file').")
    args: dict[str, Any] = Field(default_factory=dict, description="Arguments de l'outil.")


class AgentSettingsUpdate(BaseModel):
    """Mise à jour partielle des paramètres de l'agent IA.

    Champ absent ou ``null`` : inchangé. Chaîne vide pour les champs texte :
    retour à la valeur par défaut du serveur.

    SCRUM-138 : les réglages déplacés de ``app/config/settings.py`` (budgets,
    niveau de log, surface MCP, feature flags) sont désormais des clés de ce
    module de configuration IHM — stockées dans la base MongoDB et chargées
    à chaque lecture.
    """

    provider: str | None = Field(
        None, description="« ollama », « openrouter », « hf » ou « lm_studio »."
    )
    model: str | None = Field(None, max_length=200)
    ollama_url: str | None = Field(None, max_length=500)
    openrouter_url: str | None = Field(None, max_length=500)
    openrouter_api_key: str | None = Field(None, max_length=300)
    hf_url: str | None = Field(None, max_length=500)
    hf_api_key: str | None = Field(None, max_length=300)
    lm_studio_url: str | None = Field(None, max_length=500)
    timeout_seconds: float | None = Field(None, ge=10, le=3600)
    context_length: int | None = Field(None, ge=512, le=131072)
    temperature: float | None = Field(None, ge=0, le=2)
    agent_sse_first_event_timeout: int | None = Field(None, ge=1, le=300)
    agent_sse_heartbeat: int | None = Field(None, ge=1, le=120)
    # Défauts persistés du formulaire d'entraînement ML.
    train_max_per_lang: int | None = Field(None, ge=1, le=1000000)
    train_augment_fraction: float | None = Field(None, ge=0, le=1)
    train_variants_per_example: int | None = Field(None, ge=1, le=100)
    train_use_back_translation: bool | None = None
    train_epochs: int | None = Field(None, ge=1, le=100)
    train_batch_size: int | None = Field(None, ge=1, le=1024)
    train_num_workers: int | None = Field(None, ge=0, le=128)
    train_max_length: int | None = Field(None, ge=8, le=4096)
    train_learning_rate: float | None = Field(None, gt=0, le=1)
    train_weight_decay: float | None = Field(None, ge=0, le=1)
    train_warmup_ratio: float | None = Field(None, ge=0, le=1)
    train_device: str | None = Field(None, pattern="^(auto|cpu|cuda)$")
    # Déplacés de app/config/settings.py (SCRUM-138) : budgets & garde-fous.
    max_llm_rounds: int | None = Field(None, ge=1, le=50, description="Rounds LLM max par run.")
    max_tool_calls: int | None = Field(
        None, ge=1, le=200, description="Appels d'outils max par run."
    )
    # Observabilité : niveau du logger « thinktuning.agent ».
    log_level: str | None = Field(
        None, description="« DEBUG », « INFO », « WARNING » ou « ERROR »."
    )
    # Surface MCP.
    mcp_first: bool | None = Field(
        None, description="MCP-First : surface HTTP legacy de l'agent en read-only."
    )
    mcp_auth_required: bool | None = Field(
        None, description="Auth X-API-Key obligatoire sur POST /mcp/sse."
    )
    # Sécurité réseau (bac à sable SSRF) — persistés via le module IHM.
    ssrf_enabled: bool | None = Field(
        None,
        description="Blocage des hôtes privés/loopback (anti-SSRF) — actif par défaut.",
    )
    ssrf_allowlist: str | None = Field(
        None,
        max_length=500,
        description="CSV d'hôtes privés exemptés (ex. « 127.0.0.1,localhost,searxng »).",
    )
    # Feature flags (convention AGENT_<NOM> historique, désormais persistés).
    flag_reliability: bool | None = None
    flag_audit: bool | None = None
    flag_tool_analytics: bool | None = None
    flag_context: bool | None = None
    flag_copilot: bool | None = None
    flag_websocket: bool | None = None
    flag_multi_agent: bool | None = None
    flag_custom_tools: bool | None = None
    flag_new_core: bool | None = None
    flag_llm_v2: bool | None = None


class ConnectivityTestRequest(BaseModel):
    """Sonde de connectivité ; champs absents -> valeurs effectives courantes."""

    provider: str | None = None
    ollama_url: str | None = None
    openrouter_url: str | None = None
    openrouter_api_key: str | None = None
    hf_url: str | None = None
    hf_api_key: str | None = None
    lm_studio_url: str | None = None


class AgentProviderPayload(BaseModel):
    """Document Mongo complet d'une configuration provider."""

    id: str | None = Field(None, max_length=80)
    assistant: dict[str, str] = Field(
        default_factory=lambda: {"name": "Assistant IA", "status": "prêt"}
    )
    provider: dict[str, Any]
    budgets: dict[str, int] = Field(
        default_factory=lambda: {
            "max_llm_rounds_per_run": 6,
            "max_tool_calls_per_run": 20,
        }
    )
    logging: dict[str, str] = Field(default_factory=lambda: {"level": "INFO"})
    mcp: dict[str, Any] = Field(
        default_factory=lambda: {
            "surface": "MCP-First",
            "http_legacy_read_only": True,
            "auth_required": True,
        }
    )
    network_security: dict[str, Any] = Field(
        default_factory=lambda: {
            "ssrf_protection_enabled": True,
            "allowed_private_hosts": [],
        }
    )
    features: dict[str, Any] = Field(default_factory=dict)
