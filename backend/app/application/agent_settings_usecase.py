"""Use case des paramètres de l'agent (lecture / écriture / test de connectivité).

Remplace ``core/agent_settings.py`` dans la couche API : la route v1
(``api/routes/v1/agent.py``) délègue à ce use-case au lieu d'appeler
directement le store legacy.

Responsabilités :
    - ``get_effective_settings()`` : fusion SQLite + env + défauts (la base est
      prioritaire dès la première sauvegarde) ;
    - ``update_settings(values)`` : validation métier + persistance + reload ;
    - ``test_connectivity(provider, ...)`` : sonde HTTP du provider LLM.

La validation métier (``validate_settings``) vit ici, pas dans l'adaptateur :
l'adaptateur ne fait que transmettre au store legacy.
"""

from __future__ import annotations

from typing import Any

from app.config.settings import get_settings
from app.domain.ports import AgentSettingsPort

# Clés acceptées en écriture (alignées sur core/agent_settings.py::SETTING_KEYS).
SETTING_KEYS = (
    "provider",
    "model",
    "ollama_url",
    "openrouter_url",
    "openrouter_api_key",
    "hf_url",
    "hf_api_key",
    "lm_studio_url",
    "timeout_seconds",
    "context_length",
    "temperature",
)

# Valeurs par défaut (alignées sur core/agent_settings.py::VALEURS_PAR_DEFAUT).
DEFAULTS: dict[str, Any] = {
    "provider": "ollama",
    "model": "",
    "ollama_url": "",
    "openrouter_url": "",
    "openrouter_api_key": "",
    "hf_url": "",
    "hf_api_key": "",
    "lm_studio_url": "",
    "timeout_seconds": None,
    "context_length": None,
    "temperature": None,
}


def get_effective_settings(port: AgentSettingsPort) -> dict[str, Any]:
    """Config effective : priorité base > env > défauts."""
    settings = get_settings()
    values = {**DEFAULTS}

    env_values = {
        "provider": settings.agent_provider.value,
        "model": settings.agent_model_name,
        "ollama_url": settings.agent_ollama_url,
        "openrouter_url": settings.agent_openrouter_url,
        "openrouter_api_key": settings.openrouter_api_key or "",
        "hf_url": settings.agent_hf_url,
        "hf_api_key": settings.effective_hf_key or "",
        "lm_studio_url": settings.agent_lm_studio_url,
        "timeout_seconds": settings.agent_timeout_seconds,
        "context_length": settings.agent_context_length,
    }
    for key, value in env_values.items():
        if value is not None and value != "":
            values[key] = value

    persisted = port.get_all()
    values.update(persisted)
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

    effective = get_effective_settings(port)
    return effective, errors, written_keys
