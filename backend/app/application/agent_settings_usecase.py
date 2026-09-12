"""Use case des paramètres de l'agent (lecture / écriture / test de connectivité).

Remplace ``core/agent_settings.py`` dans la couche API : la route v1
(``api/routes/v1/agent.py``) délègue à ce use-case au lieu d'appeler
directement le store legacy.

Responsabilités :
    - ``get_effective_settings()`` : fusion base (MongoDB) + env + défauts (la
      base est prioritaire dès la première sauvegarde) ;
    - ``update_settings(values)`` : validation métier + persistance + reload ;
    - ``test_connectivity(provider, ...)`` : sonde HTTP du provider LLM.

La validation métier (``validate_settings``) vit ici, pas dans l'adaptateur :
l'adaptateur ne fait que transmettre au store legacy.

SCRUM-138 : ce use case est LE module de configuration IHM de l'agent — il ne
dépend plus de ``app/config/settings.py`` (aucune configuration d'agent n'y
reste) : la couche env + défauts vient de ``core.agent_settings.env_and_defaults``
et les valeurs effectives sont ENTièrement stockées / chargées depuis la base
de persistance (MongoDB via ``MongoAgentSettingsStore``).
"""

from __future__ import annotations

from typing import Any

from app.domain.ports import AgentSettingsPort

# Clés acceptées en écriture — SOURCE UNIQUE : ``core/agent_settings.py``
# (le store MongoDB ``MongoAgentSettingsStore`` filtre sur le même tuple).
from core.agent_settings import SETTING_KEYS  # noqa: F401  (ré-exporté)

# Défauts effectifs du module IHM = défauts historiques de
# app/config/settings.py (déplacés ici) : ils ne servent qu'au démarrage à
# froid, AVANT la première sauvegarde côté dashboard — ensuite la base est la
# source de vérité (page Paramètres).
DEFAULTS: dict[str, Any] = {
    "provider": "ollama",
    "model": "openrouter/free",
    "ollama_url": "http://192.168.1.184:11434/api/chat",
    "openrouter_url": "https://openrouter.ai/api/v1/chat/completions",
    "openrouter_api_key": "",
    "hf_url": "https://router.huggingface.co/v1/chat/completions",
    "hf_api_key": "",
    "lm_studio_url": "http://192.168.1.184:1234/v1/chat/completions",
    "timeout_seconds": 600,
    "context_length": 2048,
    "temperature": None,
    "train_max_per_lang": 500,
    "train_augment_fraction": 0.4,
    "train_variants_per_example": 2,
    "train_use_back_translation": False,
    "train_epochs": 4,
    "train_batch_size": 8,
    "train_num_workers": 0,
    "train_max_length": 160,
    "train_learning_rate": 3e-5,
    "train_weight_decay": 0.01,
    "train_warmup_ratio": 0.1,
    "train_device": "auto",
    # Déplacés de app/config/settings.py (SCRUM-138) :
    "max_llm_rounds": 6,
    "max_tool_calls": 20,
    # Sécurité réseau (bac à sable SSRF) — défauts fail-closed alignés sandbox.
    "ssrf_enabled": True,
    "ssrf_allowlist": "",
    "log_level": "INFO",
    "mcp_first": False,
    "mcp_auth_required": True,
    "flag_reliability": True,
    "flag_audit": True,
    "flag_tool_analytics": True,
    "flag_context": True,
    "flag_copilot": True,
    "flag_websocket": True,
    "flag_multi_agent": True,
    "flag_custom_tools": True,
    "flag_new_core": True,
    "flag_llm_v2": True,
}


def get_effective_settings(port: AgentSettingsPort) -> dict[str, Any]:
    """Config effective : priorité base > env > défauts.

    La couche env+défauts (store-free) vient de
    ``core.agent_settings.env_and_defaults`` ; seules les valeurs réellement
    issues de l'environnement remplacent les défauts du module (les ``""`` /
    ``None`` legacy ne les écrasent pas), puis les valeurs persistées en base
    s'appliquent par-dessus — une sauvegarde du dashboard est immédiatement
    effective.
    """
    from core.agent_settings import env_and_defaults

    values = {**DEFAULTS}
    for key, value in env_and_defaults().items():
        if value is not None and value != "":
            values[key] = value
    values.update(port.get_all())
    return values


def validate_settings(values: dict[str, Any]) -> list[str]:
    """Validation métier des valeurs avant sauvegarde (liste d'erreurs vide = ok)."""
    errors: list[str] = []

    provider = values.get("provider")
    if provider is not None and provider not in (
        "ollama",
        "openrouter",
        "hf",
        "lm_studio",
    ):
        errors.append("provider doit valoir 'ollama', 'openrouter', 'hf' ou 'lm_studio'.")

    timeout = values.get("timeout_seconds")
    if timeout is not None and timeout != "":
        try:
            timeout_f = float(timeout)
        except (TypeError, ValueError):
            errors.append("timeout_seconds doit être un nombre.")
        else:
            if not 10 <= timeout_f <= 3600:
                errors.append("timeout_seconds doit être entre 10 et 3600 secondes.")
            else:
                values["timeout_seconds"] = timeout_f

    context_length = values.get("context_length")
    if context_length is not None and context_length != "":
        try:
            ctx_i = int(context_length)
        except (TypeError, ValueError):
            errors.append("context_length doit être un entier.")
        else:
            if not 512 <= ctx_i <= 131072:
                errors.append("context_length doit être entre 512 et 131072 tokens.")
            else:
                values["context_length"] = ctx_i

    temperature = values.get("temperature")
    if temperature is not None and temperature != "":
        try:
            temp_f = float(temperature)
        except (TypeError, ValueError):
            errors.append("temperature doit être un nombre.")
        else:
            if not 0 <= temp_f <= 2:
                errors.append("temperature doit être entre 0 et 2.")
            else:
                values["temperature"] = temp_f

    for int_key, minimum, maximum in (
        ("train_max_per_lang", 1, 1000000),
        ("train_variants_per_example", 1, 100),
        ("train_epochs", 1, 100),
        ("train_batch_size", 1, 1024),
        ("train_num_workers", 0, 128),
        ("train_max_length", 8, 4096),
    ):
        raw = values.get(int_key)
        if raw is not None and raw != "":
            try:
                parsed = int(raw)
            except (TypeError, ValueError):
                errors.append(f"{int_key} doit être un entier.")
            else:
                if not minimum <= parsed <= maximum:
                    errors.append(f"{int_key} doit être entre {minimum} et {maximum}.")
                else:
                    values[int_key] = parsed

    for float_key, minimum, maximum in (
        ("train_augment_fraction", 0.0, 1.0),
        ("train_learning_rate", 0.0000001, 1.0),
        ("train_weight_decay", 0.0, 1.0),
        ("train_warmup_ratio", 0.0, 1.0),
    ):
        raw = values.get(float_key)
        if raw is not None and raw != "":
            try:
                parsed = float(raw)
            except (TypeError, ValueError):
                errors.append(f"{float_key} doit être un nombre.")
            else:
                if not minimum <= parsed <= maximum:
                    errors.append(f"{float_key} doit être entre {minimum} et {maximum}.")
                else:
                    values[float_key] = parsed

    train_device = values.get("train_device")
    if train_device is not None and train_device not in ("auto", "cpu", "cuda"):
        errors.append("train_device doit valoir 'auto', 'cpu' ou 'cuda'.")

    # --- Déplacés de app/config/settings.py (SCRUM-138) : budgets, log,
    # --- surface MCP et feature flags, désormais persistés via l'IHM.
    for int_key, maximum in (
        ("max_llm_rounds", 50),
        ("max_tool_calls", 200),
    ):
        raw_budget = values.get(int_key)
        if raw_budget is not None and raw_budget != "":
            try:
                budget_i = int(raw_budget)
            except (TypeError, ValueError):
                errors.append(f"{int_key} doit être un entier.")
            else:
                if not 1 <= budget_i <= maximum:
                    errors.append(f"{int_key} doit être entre 1 et {maximum}.")
                else:
                    values[int_key] = budget_i

    log_level = values.get("log_level")
    if log_level is not None and log_level != "":
        level = str(log_level).strip().upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            errors.append("log_level doit valoir 'DEBUG', 'INFO', 'WARNING' ou 'ERROR'.")
        else:
            values["log_level"] = level

    # Sécurité réseau : l'allowlist SSRF est une CSV d'hôtes nettoyée en place
    # (espaces / entrées vides) et bornée (anti-payload géant).
    ssrf_allowlist = values.get("ssrf_allowlist")
    if ssrf_allowlist is not None and ssrf_allowlist != "":
        if not isinstance(ssrf_allowlist, str):
            errors.append("ssrf_allowlist doit être une chaîne CSV d'hôtes.")
        else:
            cleaned = ", ".join(
                part.strip() for part in ssrf_allowlist.split(",") if part.strip()
            )
            if len(cleaned) > 500:
                errors.append("ssrf_allowlist ne peut pas dépasser 500 caractères.")
            else:
                values["ssrf_allowlist"] = cleaned

    for bool_key in (
        "mcp_first",
        "mcp_auth_required",
        "train_use_back_translation",
        "ssrf_enabled",
        *(
            f"flag_{name}"
            for name in (
                "reliability",
                "audit",
                "tool_analytics",
                "context",
                "copilot",
                "websocket",
                "multi_agent",
                "custom_tools",
                "new_core",
                "llm_v2",
            )
        ),
    ):
        raw_flag = values.get(bool_key)
        if raw_flag is not None and not isinstance(raw_flag, bool):
            if isinstance(raw_flag, str):
                lowered = raw_flag.strip().lower()
                if lowered in ("true", "1", "yes", "on"):
                    values[bool_key] = True
                elif lowered in ("false", "0", "no", "off", ""):
                    values[bool_key] = lowered != ""
                else:
                    errors.append(f"{bool_key} doit être un booléen (true/false/1/0/yes/no).")
            elif isinstance(raw_flag, (int, float)):
                values[bool_key] = bool(raw_flag)
            else:
                errors.append(f"{bool_key} doit être un booléen.")

    api_key = values.get("openrouter_api_key")
    if (
        provider == "openrouter"
        and api_key is not None
        and isinstance(api_key, str)
        and not api_key.strip()
    ):
        errors.append("openrouter_api_key ne peut pas être vide quand provider=openrouter.")

    hf_api_key = values.get("hf_api_key")
    if (
        provider == "hf"
        and hf_api_key is not None
        and isinstance(hf_api_key, str)
        and not hf_api_key.strip()
    ):
        errors.append("hf_api_key ne peut pas être vide quand provider=hf.")

    return errors


def update_settings(
    port: AgentSettingsPort,
    values: dict[str, Any],
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Valide puis persiste ; renvoie (config_effective, erreurs, clés_écrites)."""
    filtered = {key: values[key] for key in SETTING_KEYS if key in values}
    errors = validate_settings(filtered.copy())

    written_keys: list[str] = []
    if not errors:
        written = port.save_many(filtered)
        written_keys = sorted(written.keys())
        # Effet IMMÉDIAT côté outils réseau (sans attendre la prochaine lecture
        # de config ni un redémarrage) : la politique SSRF persistée surclasse
        # l'env ; les clés non persistées repassent sous contrôle env.
        try:
            from ia.tools.sandbox import apply_persisted_network_policy

            apply_persisted_network_policy(port.get_all())
        except ImportError:  # pragma: no cover — bac à sable facultatif
            pass

    effective = get_effective_settings(port)
    return effective, errors, written_keys
