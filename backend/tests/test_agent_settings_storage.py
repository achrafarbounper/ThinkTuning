"""Tests de la persistance des paramètres agent (SCRUM-137).

Vérifient le correctif du provider « double-encodé » (``'"openrouter"'`` avec
guillemets parasites → ``ValueError: Provider LLM inconnu`` à la construction
de ``LLMClient`` / ``HttpLLMClient``) :

    - ``MongoAgentSettingsStore`` décode puis normalise les valeurs
      JSON-encodées migrées depuis SQLite (``'"openrouter"'`` →
      ``openrouter``) et persiste les nouvelles écritures en BSON natif ;
    - ``LegacySettingsAdapter``/``build_settings_port`` résolvent le MÊME
      backend que la lecture runtime (``agent_config``) : une sauvegarde du
      dashboard est immédiatement effective ;
    - ``get_agent_settings`` durcit le provider (guillemets environnants
      retirés) — la config relue ne peut plus faire planter
      ``reload_agent_runner``.

Les tests du décodage Mongo s'appuient sur le helper PUR
(``_decode_settings_value``) — aucun MongoDB requis. Les tests de bout en
bout forcent ``PERSISTENCE_BACKEND=sqlite`` avec un store isolé par test.
"""

from __future__ import annotations

import core.agent_cache as agent_cache
import core.agent_settings as settings_store
from app.infrastructure.persistence.mongodb import _decode_settings_value

# --- Décodage Mongo (helper pur, aucun I/O) --------------------------------


def test_decode_json_encoded_string():
    """Valeur copiée telle quelle par la migration SQLite→Mongo (SCRUM-137)."""
    # Chaîne brute « "openrouter" » (guillemets compris) -> provider propre.
    assert _decode_settings_value('"openrouter"') == "openrouter"


def test_decode_json_encoded_string_keeps_string_type():
    """Un chiffre encodé en chaîne JSON (« "600.0" ») reste une chaîne."""
    assert _decode_settings_value('"600.0"') == "600.0"


def test_decode_json_number_string_becomes_number():
    """Un chiffre JSON valide (600.0) produit le même résultat que SQLite."""
    assert _decode_settings_value("600.0") == 600.0
    assert _decode_settings_value("2048") == 2048


def test_decode_raw_string_passthrough():
    """Écriture runtime native (ancien save_many Mongo) : inchangée."""
    assert _decode_settings_value("openrouter") == "openrouter"
    assert _decode_settings_value("sk-or-v1-abc") == "sk-or-v1-abc"
    assert _decode_settings_value("https://openrouter.ai/api/v1") == (
        "https://openrouter.ai/api/v1"
    )


def test_decode_typed_values_unchanged():
    """Valeurs déjà typées (int/float/None) : inchangées."""
    assert _decode_settings_value(600.0) == 600.0
    assert _decode_settings_value(2048) == 2048
    assert _decode_settings_value(None) is None


def test_mongo_store_writes_native_values_and_normalizes_legacy_documents():
    """Les documents migrés sont réparés et les nouvelles valeurs restent typées."""
    import mongomock

    from app.infrastructure.persistence.mongodb import (
        MongoAgentSettingsStore,
        MongoClientProvider,
        MongoConfig,
    )

    provider = MongoClientProvider(
        MongoConfig(uri="mongodb://localhost/thinktuning"),
        client=mongomock.MongoClient(),
    )
    collection = provider.collection("agent_settings")
    collection.insert_one({"_id": "legacy", "key": "model", "value": '"qwen3"'})
    store = MongoAgentSettingsStore(provider=provider)

    assert store.get_all()["model"] == "qwen3"
    assert collection.find_one({"_id": "legacy"})["value"] == "qwen3"

    store.save_many(
        {"provider": "lm_studio", "context_length": 2048, "mcp_first": False}
    )
    assert collection.find_one({"_id": "provider"})["value"] == "lm_studio"
    assert collection.find_one({"_id": "context_length"})["value"] == 2048
    assert collection.find_one({"_id": "mcp_first"})["value"] is False


# --- Boucle écriture → lecture (backend partagé) ---------------------------


def test_port_adapter_shares_runtime_backend(tmp_path, monkeypatch):
    """Une sauvegarde via le port (route PUT) est lue par le runtime.

    Avant SCRUM-137, ``build_settings_port`` instanciait TOUJOURS le store
    SQLite : en mode MongoDB le dashboard écrivait dans SQLite pendant que
    ``agent_config()`` relisait Mongo → réglages jamais appliqués.
    """
    monkeypatch.setenv("PERSISTENCE_BACKEND", "sqlite")
    settings_store.reset_store_for_tests(str(tmp_path / "agent_settings.db"))

    from app.infrastructure.legacy_settings_adapter import build_settings_port

    port = build_settings_port()
    assert port.save_many({"provider": "openrouter"}) == {"provider": "openrouter"}

    # La lecture du runtime (consommée par reload_agent_runner) voit la valeur.
    effective = settings_store.get_agent_settings()
    assert effective["provider"]["value"] == "openrouter"
    assert effective["provider"]["source"] == "sqlite"

    # Et la config effective de l'agent est saine (agent_cache -> LLMClient OK).
    assert agent_cache.agent_config()["provider"] == "openrouter"


def test_port_saves_runtime_compatible_values(tmp_path, monkeypatch):
    """Le port et le runtime partagent le store : aucune valeur séparée."""
    monkeypatch.setenv("PERSISTENCE_BACKEND", "sqlite")
    settings_store.reset_store_for_tests(str(tmp_path / "agent_settings.db"))

    from app.infrastructure.legacy_settings_adapter import build_settings_port

    build_settings_port().save_many(
        {"provider": "lm_studio", "timeout_seconds": 120.0, "context_length": 4096}
    )
    effective = settings_store.get_agent_settings()
    assert effective["provider"]["value"] == "lm_studio"
    assert effective["timeout_seconds"]["value"] == 120.0
    assert effective["context_length"]["value"] == 4096


# --- Durcissement du provider (guillemets parasités) ------------------------


def test_quoted_provider_in_store_is_stripped(tmp_path, monkeypatch):
    """État corrompu « "openrouter" » en base → provider propre relu.

    Simule le double-encodage resté en base (une ligne SQLite dont le
    ``json.loads`` unique rend encore une chaîne avec guillemets) : la
    normalisation doit les retirer pour que ``reload_agent_runner`` ne lève
    plus `Provider LLM inconnu`.
    """
    monkeypatch.setenv("PERSISTENCE_BACKEND", "sqlite")
    store = settings_store.reset_store_for_tests(str(tmp_path / "agent_settings.db"))

    # valeur AVEC guillemets littéraux autour de « openrouter ».
    store.save_many({"provider": '"openrouter"'})

    effective = settings_store.get_agent_settings()
    assert effective["provider"]["value"] == "openrouter"
    # Config effective utilisée par _build_runner : provider propre, donc
    # LLMClient ne lève plus ValueError.
    assert agent_cache.agent_config()["provider"] == "openrouter"


def test_quoted_provider_default_fallback(tmp_path, monkeypatch):
    """Un provider réduit à des guillemets tombe sur le défaut (ollama)."""
    monkeypatch.setenv("PERSISTENCE_BACKEND", "sqlite")
    store = settings_store.reset_store_for_tests(str(tmp_path / "agent_settings.db"))

    store.save_many({"provider": '""'})

    effective = settings_store.get_agent_settings()
    assert effective["provider"]["value"] == "ollama"
