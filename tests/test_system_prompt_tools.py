"""
Tests offline du prompt système généré depuis le registre des outils.

Contexte : sans liste d'outils dans le prompt, le modèle « devine » ses
outils et répond de mémoire au lieu d'appeler web_search / read_file
(bug : « qui a gagné la coupe du monde 2026 » sans aucune recherche web).
Lance avec : pytest tests/test_system_prompt_tools.py -v
"""

import os

# Config test AVANT tout import (le cache insère ia/ dans sys.path).
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("AGENT_OLLAMA_URL", "http://127.0.0.1:9/api/chat")  # port factice

# ORDRE IMPORTANT : importer agent_cache AVANT tout module « agent.* ».
from core import agent_cache  # noqa: E402
from agent.system_prompt import build_system_prompt, build_tools_section  # noqa: E402

AgentCore = agent_cache.AgentCore
TOOLS = agent_cache.TOOLS
REQUIRED_ARGS = agent_cache.REQUIRED_ARGS


def test_build_tools_section_lists_every_registered_tool():
    section = build_tools_section(TOOLS, REQUIRED_ARGS)
    for name in TOOLS:
        assert name in section, f"outil absent du prompt : {name}"


def test_build_tools_section_shows_required_args_and_descriptions():
    section = build_tools_section(TOOLS, REQUIRED_ARGS)
    # Signatures avec arguments requis
    assert "web_search(query" in section
    assert "- read_file(path, max_bytes=65536)" in section
    assert "- add(a, b)" in section
    # Description extraite de la docstring (1re phrase de search_in_files)
    assert "Cherche" in section


def test_build_system_prompt_contains_tools_and_usage_guidance():
    prompt = build_system_prompt(TOOLS, REQUIRED_ARGS)
    assert "OUTILS DISPONIBLES" in prompt
    assert "QUAND UTILISER UN OUTIL" in prompt
    # Règles anti-invention, orientation web_search et fidélité au résultat
    assert "DEVINER" in prompt
    assert "web_search" in prompt
    assert "REPRODUIS le retour OBTENU tel quel" in prompt
    # Le format JSON strict reste enseigné
    assert '{"tool"' in prompt or '"tool"' in prompt


def test_build_system_prompt_without_args_uses_central_registry():
    prompt = build_system_prompt()
    assert "web_search" in prompt
    assert "postgres_query" in prompt


def test_system_prompt_forbids_memory_answers_for_factual_questions():
    """Régression « qui a gagné la coupe du monde 2026 » : le prompt doit
    interdire explicitement de répondre de mémoire à un fait et imposer un
    outil (web_search) AVANT toute conclusion."""
    prompt = build_system_prompt(TOOLS, REQUIRED_ARGS)
    # Règles fermes dans la section « QUAND UTILISER UN OUTIL »
    assert "INTERDICTION DE RÉPONDRE DE MÉMOIRE" in prompt
    assert "web_search AVANT toute réponse" in prompt
    assert "coupe du monde 2026" in prompt
    assert "Dans le doute, préfère TOUJOURS appeler l'outil" in prompt
    # La réponse directe (texte normal) reste réservée aux cas sans fait externe
    assert "salutation" in prompt.lower()


def test_system_prompt_exceptions_direct_text_are_limited():
    """La réponse en texte normal sans outil ne doit être autorisée que pour
    la salutation / explication, PAS pour un fait externe."""
    prompt = build_system_prompt(TOOLS, REQUIRED_ARGS)
    lower = prompt.lower()
    assert "salutation" in lower
    assert "explication générale" in lower
    # La consigne « appeler web_search pour actualité/sport » est toujours là
    assert "sport" in lower
    assert "résultat" in lower


def test_agentcore_default_prompt_lists_real_tools():
    class _LLM:
        def call(self, messages):
            return "ok"

    prompt = AgentCore(_LLM()).system_prompt
    assert "web_search" in prompt
    assert "read_file" in prompt

    # Le mode Réflexion s'ajoute au prompt enrichi (pas de régression)
    prompt_thinking = AgentCore(_LLM(), enable_thinking=True).system_prompt
    assert prompt_thinking.startswith(prompt)
    assert "MODE RÉFLEXION" in prompt_thinking


def test_build_tools_section_promotes_targeted_edit_and_done_rule():
    """Guidance « vibecoding » : privilégier edit_file ciblé + preuve d'exécution."""
    section = build_tools_section(TOOLS, REQUIRED_ARGS)
    # Signature réelle listée + guidance conditionnelle présente
    assert "- edit_file(path, old_text, new_text, replace_all=False)" in section
    assert "Modification d'un FICHIER EXISTANT" in section
    assert "Réserve write_file" in section
    # Règle « DONE » : jamais conclure sans exécution probante
    assert "RÈGLE « DONE » (code)" in section
    assert "preuve d'exécution" in section
    assert "corrige avec edit_file" in section


def test_build_tools_section_hides_edit_guidance_without_the_tool():
    """Sans edit_file au registre : ni signature ni guidance associée (pas de
    règle annonçant un outil inexistant), mais la règle « DONE » reste car
    run_python/write_file sont toujours présents."""
    subset_tools = {k: v for k, v in TOOLS.items() if k != "edit_file"}
    section = build_tools_section(subset_tools, REQUIRED_ARGS)
    assert "- edit_file(" not in section
    assert "Modification d'un FICHIER EXISTANT" not in section
    assert "edit_file ciblé" not in section
    assert "RÈGLE « DONE » (code)" in section  # dépend de run_python+write_file