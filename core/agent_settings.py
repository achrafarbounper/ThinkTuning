# project/core/agent_settings.py

"""Paramètres persistants de l'agent IA (SQLite clé/valeur).

Même schéma de code que ``core/job_store.py`` : une base SQLite dédiée
(experiments/agent_settings.db, surchargeable via AGENT_SETTINGS_PATH) avec
une table unique ``agent_settings(key TEXT PRIMARY KEY, value TEXT,
updated_at REAL)``.

Config effective = priorité décroissante :
    1. valeurs sauvegardées en base (page Paramètres du dashboard) ;
    2. variables d'environnement (AGENT_PROVIDER, AGENT_MODEL_NAME, ...) ;
    3. défauts historiques du module.

La base est la source de vérité dès la première sauvegarde : modifier l'env
ne ré-écrase jamais une valeur explicite enregistrée côté dashboard.
``core.agent_cache.agent_config()`` consomme ce module ; les routes
``/api/agent/settings`` (GET/PUT/test) exposent la lecture / écriture /
test de connectivité.
"""

import json
import os
import sqlite3
import time

AGENT_SETTINGS_PATH = os.getenv(
    "AGENT_SETTINGS_PATH", os.path.join("experiments", "agent_settings.db")
)

# Clés acceptées en écriture (toute autre clé est ignorée silencieusement).
SETTING_KEYS = (
    "provider",
    "model",
    "ollama_url",
    "openrouter_url",
    "openrouter_api_key",
    "hf_url",
    "hf_api_key",
    "timeout_seconds",
    "context_length",
    "temperature",
)

VALEURS_PAR_DEFAUT = {
    "provider": "ollama",
    "model": "",
    "ollama_url": "",
    "openrouter_url": "",
    "openrouter_api_key": "",
    "hf_url": "",
    "hf_api_key": "",
    "timeout_seconds": None,
    "context_length": None,
    "temperature": None,
}


class AgentSettingsStore:
    """Store clé/valeur minimal au-dessus d'une table SQLite."""

    def __init__(self, path: str = AGENT_SETTINGS_PATH):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._ensure_db()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30.0)

    def _ensure_db(self):
        conn = self._connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def get_all(self) -> dict:
        """Charge toutes les paires en base (dict vide si aucune)."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT key, value FROM agent_settings").fetchall()
        finally:
            conn.close()
        return {key: json.loads(value) for key, value in rows}

    def save_many(self, values: dict) -> dict:
        """Upsert transactionnel des clés connues ; renvoie ce qui a été écrit."""
        filtered = {
            key: values[key] for key in SETTING_KEYS if key in values
        }
        if not filtered:
            return {}
        now = time.time()
        conn = self._connect()
        try:
            with conn:  # transaction atomique (commit/rollback)
                for key, value in filtered.items():
                    conn.execute(
                        """
                        INSERT INTO agent_settings(key, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value=excluded.value, updated_at=excluded.updated_at
                        """,
                        (key, json.dumps(value), now),
                    )
        finally:
            conn.close()
        return filtered


# --- Config effective ---------------------------------------------------------------

# Instance partagée (créée paresseusement au premier accès).
_store: AgentSettingsStore | None = None


def _get_store() -> AgentSettingsStore:
    global _store
    if _store is None:
        _store = AgentSettingsStore()
    return _store


def reset_store_for_tests(path: str) -> AgentSettingsStore:
    """Réinitialise le store partagé vers une base isolée (tests)."""
    global _store
    _store = AgentSettingsStore(path)
    return _store


def _hf_key_entry(stored: dict) -> dict:
    """Entrée de la clé HF : SQLite > env HF_API_KEY > env HF_TOKEN > défaut."""
    if "hf_api_key" in stored:
        return {"value": stored["hf_api_key"], "source": "sqlite"}
    for env_key in ("HF_API_KEY", "HF_TOKEN"):
        if (os.getenv(env_key) or "").strip():
            return {"value": os.getenv(env_key).strip(), "source": "env"}
    return {"value": "", "source": "default"}


def get_agent_settings() -> dict:
    """Config effective avec sa source par clé.

    Retourne ``{clé: {"value": ..., "source": "sqlite"|"env"|"default"}}`` :
    - ``provider`` : valeur brute effective ;
    - pour URLs / modèle / clé : la valeur env n'est exposée QUE si aucune
      valeur SQLite ne l'écrase (sinon chaîne vide), car la fabrique
      d'endpoint choisit déjà l'URL par défaut selon le provider ;
    - numérique non défini : ``None`` (le consommateur applique son défaut).
    """
    stored = _get_store().get_all()

    def entry(key, env_key=None, default=None):
        if key in stored:
            return {"value": stored[key], "source": "sqlite"}
        if env_key and (os.getenv(env_key) or "").strip():
            raw = os.getenv(env_key).strip()
            return {"value": raw, "source": "env"}
        return {"value": default, "source": "default"}

    settings = {
        "provider": entry("provider", "AGENT_PROVIDER", "ollama"),
        "model": entry("model", "AGENT_MODEL_NAME", ""),
        "ollama_url": entry("ollama_url", "AGENT_OLLAMA_URL", ""),
        "openrouter_url": entry("openrouter_url", "AGENT_OPENROUTER_URL", ""),
        "openrouter_api_key": entry("openrouter_api_key", "OPENROUTER_API_KEY", ""),
        # Clé Hugging Face : env HF_API_KEY en priorité, HF_TOKEN en repli
        # (c'est le nom historique du jeton côté HF).
        "hf_url": entry("hf_url", "AGENT_HF_URL", ""),
        "hf_api_key": _hf_key_entry(stored),
        "timeout_seconds": entry("timeout_seconds", "AGENT_TIMEOUT_SECONDS", None),
        "context_length": entry("context_length", "AGENT_CONTEXT_LENGTH", None),
        "temperature": entry("temperature", None, None),
    }
    # Normalisation : l'env peut porter « OpenRouter » ; les chaînes sont
    # nettoyées pour que « » == non défini côté consommateurs.
    settings["provider"]["value"] = (settings["provider"]["value"] or "ollama").strip().lower()
    for text_key in ("model", "ollama_url", "openrouter_url", "hf_url"):
        raw = settings[text_key]["value"]
        settings[text_key]["value"] = raw.strip() if isinstance(raw, str) else raw
    return settings


def save_agent_settings(values: dict) -> dict:
    """Valide puis persiste ; renvoie la config effective rechargée."""
    errors = validate_agent_settings(values)
    if errors:
        raise ValueError("; ".join(errors))
    written = _get_store().save_many(values)
    settings = get_agent_settings()
    settings["_written_keys"] = sorted(written)
    return settings


def save_many(values: dict) -> dict:
    """Raccourci d'écriture brute (sans validation) via le store partagé.

    Réservé aux tests et aux usages internes ; la route PUT passe par
    ``save_agent_settings`` qui valide au préalable.
    """
    return _get_store().save_many(values)


def validate_agent_settings(values: dict) -> list[str]:
    """Validation métier des valeurs avant sauvegarde (liste d'erreurs vide=ok)."""
    errors: list[str] = []
    provider = values.get("provider")
    if provider is not None and provider not in ("ollama", "openrouter", "hf"):
        errors.append("provider doit valoir 'ollama', 'openrouter' ou 'hf'.")

    timeout = values.get("timeout_seconds")
    if timeout is not None:
        try:
            timeout_f = float(timeout)
        except (TypeError, ValueError):
            errors.append("timeout_seconds doit être un nombre.")
        else:
            if not 10 <= timeout_f <= 3600:
                errors.append("timeout_seconds doit être entre 10 et 3600 secondes.")
            elif values.get("timeout_seconds") is not None:
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
        # Une sauvegarde explicite d'une clé vide alors que openrouter est
        # choisi est refusée : elle rendrait tout appel LLM impossible.
        errors.append(
            "openrouter_api_key ne peut pas être vide quand provider=openrouter."
        )
    hf_api_key = values.get("hf_api_key")
    if (
        provider == "hf"
        and hf_api_key is not None
        and isinstance(hf_api_key, str)
        and not hf_api_key.strip()
    ):
        # Même règle qu'OpenRouter : une clé vide explicite rendrait tout
        # appel LLM impossible côté Hugging Face Inference Providers.
        errors.append(
            "hf_api_key ne peut pas être vide quand provider=hf."
        )
    return errors