"""Tests offline du parser JSON et de la boucle d'auto-correction de l'agent.

Couvre les correctifs du bug « l'agent n'affiche pas le contenu du fichier » :
    - extraction JSON robuste (prose autour, fences markdown, multi-blocs) ;
    - auto-correction : tool inconnu / sans JSON / args manquants renvoyés au LLM ;
    - fidélité de la réponse finale (contenu reproduit, interdiction d'inventer).

Aucun réseau : le LLM est remplacé par un ScriptedLLM déterministe.
Lance avec : pytest tests/test_agent_parser_loop.py -v
"""

import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IA_DIR = os.path.join(PROJECT_ROOT, "ia")
for _p in (PROJECT_ROOT, IA_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("AGENT_API_KEY", "test-agent-key")

import pytest

from agent.json_parser import extract_json_blocks


# --- Parser JSON -----------------------------------------------------------------

def test_parser_extracts_json_after_prose_containing_odd_quotes():
    """Régression : un nombre impair de guillemets dans la prose précédente
    corrompait l'analyseur à état global -> le JSON n'était plus détecté."""
    text = (
        'L\'utilisateur a dit "bonjour puis lit le fichier :\n'
        '{"tool": "read_file", "args": {"path": "README.md"}}'
    )
    assert extract_json_blocks(text) == [
        {"tool": "read_file", "args": {"path": "README.md"}}
    ]


def test_parser_handles_markdown_fences():
    text = '```json\n{"tool": "add", "args": {"a": 1, "b": 2}}\n```'
    assert extract_json_blocks(text) == [{"tool": "add", "args": {"a": 1, "b": 2}}]


def test_parser_returns_multiple_blocks():
    text = (
        '{"tool": "add", "args": {"a": 1, "b": 2}}\n'
        'Puis : {"tool": "gpu_info", "args": {}}'
    )
    assert extract_json_blocks(text) == [
        {"tool": "add", "args": {"a": 1, "b": 2}},
        {"tool": "gpu_info", "args": {}},
    ]


def test_parser_ignores_malformed_but_keeps_following_valid_block():
    text = '{"tool": "add", "args": {"a": 1,   \n' \
           '{"tool": "read_file", "args": {"path": "x"}}'
    blocks = extract_json_blocks(text)
    assert {"tool": "read_file", "args": {"path": "x"}} in blocks


def test_parser_tolerates_braces_inside_strings():
    payload = {
        "tool": "run_python",
        "args": {"code": 'print("{not a json block}")'},
    }
    assert extract_json_blocks(json.dumps(payload)) == [payload]


def test_parser_plain_text_returns_empty_list():
    assert extract_json_blocks("Bonjour ! Pas de JSON ici.") == []
    assert extract_json_blocks("") == []


# --- Boucle d'auto-correction ------------------------------------------------------

class ScriptedLLM:
    """LLM factice : renvoie les réponses fournies, mémorise les prompts."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def call(self, messages):
        self.prompts.append(messages[-1]["content"])
        if self.replies:
            return self.replies.pop(0)
        return "Explication finale par défaut."


@pytest.fixture()
def sandbox_root(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path))
    return tmp_path

def test_loop_self_corrects_unknown_tool_then_displays_content(sandbox_root):
    """Scénario utilisateur : 'cat' inexistant -> corrigé en read_file ->
    le contenu réel du fichier est injecté dans le prompt final."""
    from agent.agent_core import AgentCore
    from tools.file_tools import write_file

    write_file("notes/a.txt", "CONTENU-VISIBLE-A-L-ECRAN")

    llm = ScriptedLLM([
        '{"tool": "cat", "args": {"path": "notes/a.txt"}}',
        '{"tool": "read_file", "args": {"path": "notes/a.txt"}}',
        "Voici le contenu demandé.",
    ])
    answer = AgentCore(llm).run("affiche le fichier notes/a.txt")

    assert answer == "Voici le contenu demandé."
    # Appel 2 : feedback d'auto-correction avec la liste des outils valides.
    assert "[auto-correction]" in llm.prompts[1]
    assert "Tool inconnu : 'cat'." in llm.prompts[1]
    assert '"read_file"' in llm.prompts[1] or "'read_file'" in llm.prompts[1]
    # Appel 3 : synthèse fidèle — le contenu réel est injecté au LLM.
    assert llm.prompts[2].startswith("Dernier résultat")
    assert "CONTENU-VISIBLE-A-L-ECRAN" in llm.prompts[2]


def test_loop_self_corrects_when_llm_answers_without_json(sandbox_root):
    from agent.agent_core import AgentCore

    llm = ScriptedLLM([
        "Je vais lire le fichier tout de suite.",
        '{"tool": "add", "args": {"a": 2, "b": 3}}',
        "Deux plus trois font cinq.",
    ])
    assert AgentCore(llm).run("calcule 2+3") == "Deux plus trois font cinq."
    assert "aucun objet JSON exploitable" in llm.prompts[1]


def test_loop_self_corrects_missing_arguments(sandbox_root):
    from agent.agent_core import AgentCore

    llm = ScriptedLLM([
        '{"tool": "read_file", "args": {}}',
        '{"tool": "read_file", "args": {"path": "f.txt"}}',
        "Contenu affiché.",
    ])
    assert AgentCore(llm).run("lis f.txt") == "Contenu affiché."
    assert "Arguments manquants pour read_file : ['path']" in llm.prompts[1]


def test_loop_gives_up_after_max_rounds_without_json():
    from agent.agent_core import MAX_TOOL_ROUNDS, AgentCore

    llm = ScriptedLLM(["réponse hors JSON"] * MAX_TOOL_ROUNDS)
    answer = AgentCore(llm).run("salut")
    assert answer.startswith("Réponse non exploitable")
    assert len(llm.prompts) == MAX_TOOL_ROUNDS  # pas d'appel de synthèse


def test_loop_gives_up_after_max_rounds_with_unknown_tool():
    from agent.agent_core import MAX_TOOL_ROUNDS, AgentCore

    llm = ScriptedLLM(['{"tool": "division", "args": {"a": 1}}'] * MAX_TOOL_ROUNDS)
    answer = AgentCore(llm).run("divise")
    assert "Tool inconnu : 'division'." in answer
    assert "tentatives d'auto-correction" in answer
    assert len(llm.prompts) == MAX_TOOL_ROUNDS


def test_final_prompt_forbids_inventing_content(sandbox_root):
    """Le prompt de synthèse impose la citation verbatim, interdit d'inventer."""
    from agent.agent_core import AgentCore

    llm = ScriptedLLM([
        '{"tool": "add", "args": {"a": 1, "b": 2}}',
        "1 + 2 = 3.",
    ])
    AgentCore(llm).run("calcule")
    final_prompt = llm.prompts[1]
    assert final_prompt.startswith("Dernier résultat : 3.")
    assert "TELLE QUELLE" in final_prompt
    assert "invente JAMAIS" in final_prompt
    assert "triples backticks" in final_prompt


# --- System prompt généré depuis le registre ----------------------------------------

def test_system_prompt_lists_every_registered_tool():
    from agent.system_prompt import build_system_prompt
    from tools.tool_registry import TOOLS

    prompt = build_system_prompt()
    for name in TOOLS:
        assert f"- {name}(" in prompt


def test_system_prompt_read_file_signature_and_fidelity_rules():
    from agent.system_prompt import build_system_prompt

    prompt = build_system_prompt()
    assert "- read_file(path, max_bytes=65536)" in prompt
    assert "REPRODUIS le retour OBTENU tel quel" in prompt
    assert "invente JAMAIS" in prompt


def test_system_prompt_is_regenerated_when_registry_changes(monkeypatch):
    """Un outil ajouté au registre apparaît automatiquement dans le prompt."""
    import tools.tool_registry as registry
    from agent.system_prompt import build_system_prompt

    def fake_tool(x):
        """Fait un truc factice."""
        return x

    monkeypatch.setitem(registry.TOOLS, "fake_tool_x", fake_tool)
    monkeypatch.setitem(registry.REQUIRED_ARGS, "fake_tool_x", ["x"])

    prompt = build_system_prompt()
    assert "- fake_tool_x(x)   # Fait un truc factice." in prompt