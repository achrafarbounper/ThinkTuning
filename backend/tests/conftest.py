"""Fixtures globales des tests backend.

Isolation du store de paramètres de l'agent (module de configuration IHM,
``core/agent_settings.py``) : chaque test part d'une base vide (SQLite
temporaire, backend ``sqlite`` forcé) — les tests qui vérifient une valeur
POSÉE en base passent leur propre store isolé. Sans cette isolation, un
``experiments/agent_settings.db`` de développement (ou un
``PERSISTENCE_BACKEND=mongodb`` local) ferait flakker les tests de défauts :
en production (SCRUM-138), les valeurs de configuration de l'agent sont
entièrement stockées et chargées depuis MongoDB — la lecture runtime DOIT
retrouver la base, jamais l'état local d'une machine.
"""

from __future__ import annotations

import os

os.environ.setdefault("MONGODB_MOCK", "1")
os.environ.setdefault("MONGODB_URI", "mongodb://localhost")
os.environ.setdefault("MONGODB_DATABASE", "thinktuning_test")

import pytest


@pytest.fixture(autouse=True)
def _clear_app_settings_cache():
    """Vide le cache ``get_settings()`` autour de chaque test (déterminisme).

    Le cache est partagé par toute la session pytest : sans ce nettoyage, un
    test qui modifie l'environnement hériterait des Settings d'un autre test.
    """
    from app.config.settings import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolated_agent_settings_store(monkeypatch, tmp_path):
    """Store de paramètres agent VIDE + backend sqlite forcé, par test.

    Le mode Mongo (``PERSISTENCE_BACKEND=mongodb``) est désactivé : aucun test
    n'a besoin d'un Atlas réel, et un .env local ne doit pas faire flakker la
    suite. Le chemin du store est restauré en sortie (les tests de module qui
    repointent eux-mêmes ``reset_store_for_tests`` restent indépendants).
    """
    from core import agent_settings as agent_settings_module

    monkeypatch.setenv("PERSISTENCE_BACKEND", "mongodb")
    monkeypatch.setenv("MONGODB_MOCK", "1")
    from app.infrastructure.persistence.mongodb import reset_mongo_provider
    reset_mongo_provider()
    original_path = agent_settings_module.AGENT_SETTINGS_PATH
    agent_settings_module.reset_store_for_tests(str(tmp_path / "agent_settings.db"))
    yield
    reset_mongo_provider()
    agent_settings_module.reset_store_for_tests(original_path)
