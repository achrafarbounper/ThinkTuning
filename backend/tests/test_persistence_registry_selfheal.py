# project/tests/test_persistence_registry_selfheal.py
"""Garde-fous du registre late-binding des stores Mongo (ADR-0004).

Couvre le self-heal ajouté dans ``persistence/common.get_mongo_store_class`` :
quand ``app.infrastructure.persistence.mongodb`` n'a jamais été importé dans le
process (ex. ``uvicorn app.api.main:app`` lancé directement), le registre des
implémentations Mongo est vide et toute résolution levait un ``RuntimeError``
dès le premier accès à un store (SCRUM-137 : ``agent_settings`` en premier).
Désormais la résolution déclenche l'import paresseux du module, qui
s'enregistre lui-même au chargement.

Herméticité : AUCUN test ne ré-importe réellement le module (les classes seraient
re-créées et l'attribut ``mongodb`` du package parent re-relié, casse-fouillis
pour les autres tests). L'import est simulé via ``monkeypatch`` sur
``importlib.import_module`` ; le registre est substitué par monkeypatch aussi.

Aucun réseau : ``MONGODB_MOCK=1`` est posé par ``tests/conftest.py``.

Lance : pytest tests/test_persistence_registry_selfheal.py -v
"""

from __future__ import annotations

import importlib
import sys
import types

from app.infrastructure.persistence import common
from app.infrastructure.persistence.mongodb import MongoAgentSettingsStore

MONGODB_MODULE = "app.infrastructure.persistence.mongodb"


def test_registry_miss_triggers_lazy_import_and_resolves(monkeypatch):
    """Registre vide + module non chargé → import paresseux → résolution OK.

    ``importlib.import_module`` est substitué : il simule l'enregistrement des
    classes au chargement de ``persistence.mongodb`` (ce que fait réellement
    le bloc ``register_mongo_store(...)`` en fin de module).
    """
    monkeypatch.setattr(common, "_MONGO_STORE_CLASSES", {})  # registre vide
    monkeypatch.delitem(sys.modules, MONGODB_MODULE, raising=False)
    calls: list[str] = []

    def fake_import(name: str):
        calls.append(name)
        assert name == MONGODB_MODULE
        common._MONGO_STORE_CLASSES["agent_settings"] = MongoAgentSettingsStore
        return types.ModuleType(name)

    monkeypatch.setattr(importlib, "import_module", fake_import)

    cls = common.get_mongo_store_class("agent_settings")

    assert calls == [MONGODB_MODULE]  # le self-heal a bien déclenché l'import
    assert cls is MongoAgentSettingsStore


def test_registry_resolves_all_registered_keys_without_import(monkeypatch):
    """Les 5 clés historiques se résolvent depuis le registre (sans ré-import)."""
    from app.infrastructure.persistence import mongodb as mongodb_module

    expected = {
        "audit": mongodb_module.MongoAuditStore,
        "run": mongodb_module.MongoRunStore,
        "flow": mongodb_module.MongoFlowStore,
        "agent_settings": mongodb_module.MongoAgentSettingsStore,
        "mcp_client": mongodb_module.MongoMCPClientStore,
    }
    monkeypatch.setattr(common, "_MONGO_STORE_CLASSES", dict(expected))

    for key, cls in expected.items():
        assert common.get_mongo_store_class(key) is cls


def test_agent_settings_factory_resolves_mongo_store_without_prealoaded_registry(
    monkeypatch,
):
    """Bout-en-bout : ``get_settings_store()`` rend un store Mongo SANS import préalable.

    Reproduit la séquence de production : démarrage direct
    (``uvicorn app.api.main:app``), ``PERSISTENCE_BACKEND=mongodb``, premier
    accès au store des paramètres. L'import paresseux du module Mongo est
    simulé (cf. docstring du module) : il enregistre l'implémentation réelle
    (classe liée au chargement du module de test — identité stable). Avec
    ``MONGODB_MOCK=1`` (conftest), aucune connexion réseau n'est ouverte.
    """
    from app.infrastructure.persistence import agent_settings as agent_settings_module

    monkeypatch.setattr(common, "_MONGO_STORE_CLASSES", {})  # registre vide
    monkeypatch.delitem(sys.modules, MONGODB_MODULE, raising=False)

    def fake_import(name: str):
        assert name == MONGODB_MODULE
        common._MONGO_STORE_CLASSES["agent_settings"] = MongoAgentSettingsStore
        return types.ModuleType(name)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    monkeypatch.setenv("PERSISTENCE_BACKEND", "mongodb")
    monkeypatch.setenv("MONGODB_MOCK", "1")
    monkeypatch.setattr(agent_settings_module, "_mongo_store", None)

    store = agent_settings_module.get_settings_store()

    assert isinstance(store, MongoAgentSettingsStore)
    # get_all sur mongomock : base vide → dict (parité de contrat avec SQLite).
    assert store.get_all() == {}
