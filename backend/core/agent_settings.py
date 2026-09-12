# project/core/agent_settings.py

"""Paramètres persistants de l'agent IA — module de configuration de l'IHM.

Même schéma de code que ``core/job_store.py`` : une base dédiée
(experiments/agent_settings.db en SQLite, surchargeable via AGENT_SETTINGS_PATH)
ou, en mode ``PERSISTENCE_BACKEND=mongodb``, la collection ``agent_settings`` de
MongoDB (``MongoAgentSettingsStore``) — le MÊME backend que tous les autres
stores. Les valeurs de configuration de l'agent sont donc ENTièrement stockées
et rechargées depuis la base de persistance : ``app/config/settings.py`` ne
porte plus AUCUNE configuration d'agent (migration SCRUM-138).

Schéma persisté (clé/valeur JSON, ``SETTING_KEYS``) :
    - connexion LLM : provider, model, ollama_url, openrouter_url,
      openrouter_api_key, hf_url, hf_api_key, lm_studio_url ;
    - réglages d'appel : timeout_seconds, context_length, temperature ;
    - budgets & garde-fous : max_llm_rounds, max_tool_calls ;
    - sécurité réseau (bac à sable SSRF) : ssrf_enabled, ssrf_allowlist ;
    - observabilité : log_level ;
    - surface MCP : mcp_first, mcp_auth_required ;
    - feature flags : flag_<nom> (AGENT_<NOM> historique).

Config effective = priorité décroissante :
    1. valeurs sauvegardées en base (page Paramètres du dashboard) ;
    2. variables d'environnement (AGENT_PROVIDER, AGENT_MODEL_NAME, ...) —
       repli de compatibilité CI/déploiement, JAMAIS un codage en dur ici ;
    3. défauts historiques du module.

La base est la source de vérité dès la première sauvegarde : modifier l'env
ne ré-écrase jamais une valeur explicite enregistrée côté dashboard.
``core.agent_cache.agent_config()`` consomme ce module ; les routes
``/api/agent/settings`` (GET/PUT/test) exposent la lecture / écriture /
test de connectivité ; le noyau agentique v2 le consomme via
``app.agent.settings.get_agent_config()`` (modèle typé ``AgentConfig``).
"""

import json
import os
import sqlite3
import time
from typing import Any

AGENT_SETTINGS_PATH = os.getenv(
    "AGENT_SETTINGS_PATH", os.path.join("experiments", "agent_settings.db")
)

# Clés acceptées en écriture (toute autre clé est ignorée silencieusement).
SETTING_KEYS = (
    # --- Connexion LLM ------------------------------------------------------
    "provider",
    "model",
    "ollama_url",
    "openrouter_url",
    "openrouter_api_key",
    "hf_url",
    "hf_api_key",
    "lm_studio_url",
    # --- Réglages d'appel ---------------------------------------------------
    "timeout_seconds",
    "context_length",
    "temperature",
    # --- Budgets & garde-fous (déplacés de app/config/settings.py) -----------
    "max_llm_rounds",
    "max_tool_calls",
    # --- Sécurité réseau (bac à sable SSRF, page Paramètres) ----------------
    "ssrf_enabled",
    "ssrf_allowlist",
    # --- Observabilité -------------------------------------------------------
    "log_level",
    # --- Surface MCP (déplacés de app/config/settings.py) --------------------
    "mcp_first",
    "mcp_auth_required",
    # --- Feature flags (AGENT_<NOM> historique) ------------------------------
    "flag_reliability",
    "flag_audit",
    "flag_tool_analytics",
    "flag_context",
    "flag_copilot",
    "flag_websocket",
    "flag_multi_agent",
    "flag_custom_tools",
    "flag_new_core",
    "flag_llm_v2",
)

# Noms des feature flags (suffixe de la clé ``flag_<nom>`` ; env ``AGENT_<NOM>``).
FLAG_NAMES = (
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

# Clés booléennes : la valeur persistée (JSON) et la valeur env sont coercées
# en bool pour que les consommateurs (payload IHM, AgentConfig) reçoivent un
# booléen réel — jamais la chaîne "true" issue de l'environnement.
_BOOL_KEYS = ("mcp_first", "mcp_auth_required", "ssrf_enabled") + tuple(
    f"flag_{name}" for name in FLAG_NAMES
)

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})

VALEURS_PAR_DEFAUT: dict[str, Any] = {
    # Connexion LLM : défauts vides — le consommateur applique le défaut
    # dépendant du provider (cf. core/agent_cache.agent_config).
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
    # Déplacés de app/config/settings.py (défauts historiques identiques) :
    "max_llm_rounds": None,
    "max_tool_calls": None,
    "log_level": None,
    # Sécurité réseau (bac à sable SSRF) : protection ACTIVE par défaut
    # (fail-closed — même sémantique que ``AGENT_BLOCK_PRIVATE_HOSTS``) ;
    # allowlist vide = suivre l'env ``AGENT_PRIVATE_HOST_ALLOWLIST``.
    "ssrf_enabled": True,
    "ssrf_allowlist": "",
    # Surface MCP : read-only désactivé, auth transport obligatoire (fail-closed).
    "mcp_first": False,
    "mcp_auth_required": True,
    # Flags : défauts True (bascules en production terminées — historique
    # de app/config/settings.py ; ``AGENT_<NOM>=0`` conserve le repli).
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

# Cache du store Mongo (mode PERSISTENCE_BACKEND=mongodb) : instancié UNE seule
# fois puis réutilisé — même sémantique que le singleton SQLite (_store) au-dessus.
# Annoté avec le type de retour du getter (la classe ``MongoAgentSettingsStore``
# n'est importable qu'en lazy : import de module circulaire).
_mongo_store: "AgentSettingsStore | None" = None


def _get_store() -> AgentSettingsStore:
    global _store
    if os.getenv("PERSISTENCE_BACKEND", "mongodb").lower() == "mongodb":
        from app.infrastructure.persistence.mongodb import MongoAgentSettingsStore

        global _mongo_store
        if _mongo_store is None:
            _mongo_store = MongoAgentSettingsStore()  # type: ignore[assignment]
        return _mongo_store  # type: ignore[return-value]
    if _store is None:
        _store = AgentSettingsStore()
    return _store


def get_settings_store() -> AgentSettingsStore:
    """Store persistant des paramètres (SQLite ou MongoDB selon PERSISTENCE_BACKEND).

    Point d'accès PUBLIC unique (SCRUM-137) : la lecture
    (``get_agent_settings``, consommée par ``core/agent_cache.agent_config``)
    et les adaptateurs d'écriture (``LegacySettingsAdapter``) doivent résoudre
    le MÊME backend — sans quoi une sauvegarde du dashboard reste invisible du
    runtime et le rechargement de l'agent s'appuie sur des valeurs périmées.
    """
    return _get_store()


def reset_store_for_tests(path: str) -> AgentSettingsStore:
    """Réinitialise le store partagé vers une base isolée (tests)."""
    global _store
    _store = AgentSettingsStore(path)
    return _store


def _env_bool(env_key: str) -> bool | None:
    """Valeur booléenne d'une variable d'environnement (None si absente).

    Convention ``AGENT_<NOM>`` : « 1 / true / yes / on » (insensible à la
    casse) signifie activé ; toute valeur définie autre (dont « 0 / false »)
    signifie désactivé.
    """
    raw = os.getenv(env_key)
    if raw is None:
        return None
    return raw.strip().lower() in _TRUE_VALUES


def env_and_defaults() -> dict[str, Any]:
    """Couche env > défauts, SANS lecture de la base (store-free).

    Renvoie un dict plat ``{clé: valeur}`` pour TOUTES les clés de
    ``SETTING_KEYS`` — les valeurs non définies dans l'environnement restent
    aux défauts du module (``""``/``None`` pour la connexion LLM : le
    consommateur applique alors son défaut dépendant du provider). Sert de
    couche commune aux deux consommateurs de la configuration :
    ``get_agent_settings()`` (runtime legacy) et le noyau agentique v2
    (``app.agent.settings.get_agent_config()`` + use case
    ``app.application.agent_settings_usecase``, qui fusionnent ensuite les
    valeurs persistées par-dessus).
    """
    values: dict[str, Any] = dict(VALEURS_PAR_DEFAUT)

    # Clé Hugging Face : env HF_API_KEY en priorité, HF_TOKEN en repli (c'est
    # le nom historique du jeton côté HF) — même règle que ``_hf_key_entry``.
    for env_key in ("HF_API_KEY", "HF_TOKEN"):
        value = (os.getenv(env_key) or "").strip()
        if value:
            values["hf_api_key"] = value
            break

    # Chaînes / numériques : l'env n'est prise en compte que si réellement
    # définie et non vide (sinon le défaut du module s'applique).
    _env_keys = {
        "provider": "AGENT_PROVIDER",
        "model": "AGENT_MODEL_NAME",
        "ollama_url": "AGENT_OLLAMA_URL",
        "openrouter_url": "AGENT_OPENROUTER_URL",
        "openrouter_api_key": "OPENROUTER_API_KEY",
        "hf_url": "AGENT_HF_URL",
        "lm_studio_url": "AGENT_LM_STUDIO_URL",
        "timeout_seconds": "AGENT_TIMEOUT_SECONDS",
        "context_length": "AGENT_CONTEXT_LENGTH",
        "max_llm_rounds": "AGENT_MAX_LLM_ROUNDS",
        "max_tool_calls": "AGENT_MAX_TOOL_CALLS",
        "log_level": "AGENT_LOG_LEVEL",
        # Sécurité réseau (bac à sable SSRF) — page Paramètres > env.
        "ssrf_allowlist": "AGENT_PRIVATE_HOST_ALLOWLIST",
    }
    for key, env_key in _env_keys.items():
        raw = (os.getenv(env_key) or "").strip()
        if raw:
            values[key] = raw

    # Coercion numérique (même contrat que l'ancien pydantic Settings) : une
    # valeur parsable est typée ; sinon la chaîne brute est conservée (le
    # consommateur typé lèvera une erreur explicite, le payload l'affichera).
    for key, cast in (
        ("timeout_seconds", float),
        ("context_length", int),
        ("max_llm_rounds", int),
        ("max_tool_calls", int),
    ):
        raw_value = values[key]
        if isinstance(raw_value, str):
            try:
                values[key] = cast(float(raw_value)) if cast is float else int(float(raw_value))
            except (TypeError, ValueError):
                pass

    # Bools explicites : une variable définie mais falsy (« 0 ») désactive —
    # seule l'ABSENCE laisse le défaut s'appliquer.
    for key in _BOOL_KEYS:
        env_key = (
            key.upper()
            if not key.startswith("flag_")
            else f"AGENT_{key[len('flag_'):].upper()}"
        )
        flag_value = _env_bool(env_key)
        if flag_value is not None:
            values[key] = flag_value

    # Compatibilité historique : la protection SSRF se désactivait via
    # ``AGENT_BLOCK_PRIVATE_HOSTS=0`` — repli conservé quand ``SSRF_ENABLED``
    # (nom canonique de la clé) est absent de l'environnement.
    if "SSRF_ENABLED" not in os.environ:
        raw = os.getenv("AGENT_BLOCK_PRIVATE_HOSTS", "").strip().lower()
        if raw:
            values["ssrf_enabled"] = raw in _TRUE_VALUES
    return values


def _hf_key_entry(stored: dict) -> dict:
    """Entrée de la clé HF : base > env HF_API_KEY > env HF_TOKEN > défaut."""
    if "hf_api_key" in stored:
        return {"value": stored["hf_api_key"], "source": "sqlite"}
    for env_key in ("HF_API_KEY", "HF_TOKEN"):
        value = os.getenv(env_key) or ""
        if value.strip():
            return {"value": value.strip(), "source": "env"}
    return {"value": "", "source": "default"}


def _bool_entry(key: str, env_key: str, stored: dict, default: bool) -> dict:
    """Entrée booléenne : base > env (coercée) > défaut."""
    if key in stored:
        raw = stored[key]
        if isinstance(raw, bool):
            return {"value": raw, "source": "sqlite"}
        return {"value": str(raw).strip().lower() in _TRUE_VALUES, "source": "sqlite"}
    flag_value = _env_bool(env_key)
    if flag_value is not None:
        return {"value": flag_value, "source": "env"}
    return {"value": default, "source": "default"}


def _ssrf_enabled_entry(stored: dict) -> dict:
    """Entrée ssrf_enabled : base > env ``SSRF_ENABLED`` > env historique > ON."""
    if "ssrf_enabled" in stored:
        raw = stored["ssrf_enabled"]
        if isinstance(raw, bool):
            return {"value": raw, "source": "sqlite"}
        return {"value": str(raw).strip().lower() in _TRUE_VALUES, "source": "sqlite"}
    flag_value = _env_bool("SSRF_ENABLED")
    if flag_value is not None:
        return {"value": flag_value, "source": "env"}
    # Repli historique : AGENT_BLOCK_PRIVATE_HOSTS (0/false = désactivée).
    legacy = _env_bool("AGENT_BLOCK_PRIVATE_HOSTS")
    if legacy is not None:
        return {"value": legacy, "source": "env"}
    return {"value": True, "source": "default"}


def get_agent_settings() -> dict:
    """Config effective avec sa source par clé.

    Retourne ``{clé: {"value": ..., "source": "sqlite"|"env"|"default"}}`` :
    - ``provider`` : valeur brute effective ;
    - pour URLs / modèle / clé : la valeur env n'est exposée QUE si aucune
      valeur en base ne l'écrase (sinon chaîne vide), car la fabrique
      d'endpoint choisit déjà l'URL par défaut selon le provider ;
    - numérique non défini : ``None`` (le consommateur applique son défaut) ;
    - bools (flags, MCP) : toujours coercés en ``bool`` réel.

    Note historique : la source s'appelle toujours ``"sqlite"`` par souci de
    compatibilité du contrat HTTP (le dashboard compare ces libellés) — en
    mode ``PERSISTENCE_BACKEND=mongodb`` il s'agit bien de la base MongoDB.
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
        # LM Studio : serveur local SANS clé — seule l'URL est configurable.
        "lm_studio_url": entry("lm_studio_url", "AGENT_LM_STUDIO_URL", ""),
        "timeout_seconds": entry("timeout_seconds", "AGENT_TIMEOUT_SECONDS", None),
        "context_length": entry("context_length", "AGENT_CONTEXT_LENGTH", None),
        "temperature": entry("temperature", None, None),
        # Déplacés de app/config/settings.py (SCRUM-138) : mêmes clés d'env,
        # la base (MongoDB ou SQLite) reste prioritaire.
        "max_llm_rounds": entry("max_llm_rounds", "AGENT_MAX_LLM_ROUNDS", None),
        "max_tool_calls": entry("max_tool_calls", "AGENT_MAX_TOOL_CALLS", None),
        "log_level": entry("log_level", "AGENT_LOG_LEVEL", None),
        "mcp_first": _bool_entry("mcp_first", "MCP_FIRST", stored, False),
        "mcp_auth_required": _bool_entry(
            "mcp_auth_required", "MCP_AUTH_REQUIRED", stored, True
        ),
        # Sécurité réseau (bac à sable SSRF) : base > env > défaut fail-closed.
        "ssrf_enabled": _ssrf_enabled_entry(stored),
        "ssrf_allowlist": entry("ssrf_allowlist", "AGENT_PRIVATE_HOST_ALLOWLIST", ""),
    }
    for name in FLAG_NAMES:
        key = f"flag_{name}"
        settings[key] = _bool_entry(key, f"AGENT_{name.upper()}", stored, True)
    # Normalisation : l'env peut porter « OpenRouter » ; les chaînes sont
    # nettoyées pour que « » == non défini côté consommateurs. Des guillemets
    # environnants survivent à un double-encodage JSON (ex. valeur migrée
    # « "openrouter" » restée en base) : retirés en durcissement (SCRUM-137)
    # pour que LLMClient ne lève jamais « Provider LLM inconnu ».
    raw_provider = settings["provider"]["value"] or "ollama"
    settings["provider"]["value"] = (
        str(raw_provider).strip().strip("\"'").lower() or "ollama"
    )
    for text_key in ("model", "ollama_url", "openrouter_url", "hf_url", "lm_studio_url"):
        raw = settings[text_key]["value"]
        settings[text_key]["value"] = raw.strip() if isinstance(raw, str) else raw
    # Pilotage runtime du bac à sable (effet immédiat pour les outils réseau,
    # sans redémarrage) : seules les valeurs PERSISTÉES surclassent l'env.
    try:
        from ia.tools.sandbox import apply_persisted_network_policy

        apply_persisted_network_policy(stored)
    except ImportError:  # pragma: no cover — bac à sable facultatif
        pass
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
    if provider is not None and provider not in (
        "ollama", "openrouter", "hf", "lm_studio"
    ):
        errors.append(
            "provider doit valoir 'ollama', 'openrouter', 'hf' ou 'lm_studio'."
        )

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

    # --- Déplacés de app/config/settings.py (SCRUM-138) : budgets, log,
    # --- surface MCP et feature flags, désormais persistés via l'IHM.
    for int_key, _label, maximum in (
        ("max_llm_rounds", "rounds LLM max par run", 50),
        ("max_tool_calls", "appels d'outils max par run", 200),
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
            errors.append(
                "log_level doit valoir 'DEBUG', 'INFO', 'WARNING' ou 'ERROR'."
            )
        else:
            values["log_level"] = level

    for bool_key in _BOOL_KEYS:
        raw_flag = values.get(bool_key)
        if raw_flag is not None and not isinstance(raw_flag, bool):
            if isinstance(raw_flag, str):
                lowered = raw_flag.strip().lower()
                if lowered in ("",) or lowered in _TRUE_VALUES | {"false", "0", "no", "off"}:
                    values[bool_key] = lowered in _TRUE_VALUES
                else:
                    errors.append(
                        f"{bool_key} doit être un booléen (true/false/1/0/yes/no)."
                    )
            elif isinstance(raw_flag, (int, float)):
                values[bool_key] = bool(raw_flag)
            else:
                errors.append(f"{bool_key} doit être un booléen.")

    # Sécurité réseau : l'allowlist SSRF est une CSV d'hôtes — nettoyée en
    # place (espaces / entrées vides) et bornée (anti-payload géant).
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
