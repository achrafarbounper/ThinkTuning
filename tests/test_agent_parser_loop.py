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

    # La conclusion s'appuie sur le contenu réellement lu ; l'incident
    # « cat » reste tracé en toute transparence via le suffixe historique.
    assert answer.startswith("Voici le contenu demandé.")
    assert "[auto-correction] Tool inconnu : 'cat'" in answer
    # Appel 2 : feedback d'auto-correction avec la liste des outils valides.
    assert "Tool inconnu : 'cat'." in llm.prompts[1]
    assert "read_file" in llm.prompts[1]
    # Appel 3 : synthèse fidèle — le contenu réel est injecté au LLM.
    assert llm.prompts[2].startswith("Dernier résultat")
    assert "CONTENU-VISIBLE-A-L-ECRAN" in llm.prompts[2]


def test_first_round_plain_text_without_tool_mention_is_direct_answer():
    """Réponse en TEXTE NORMAL au premier tour sans aucune mention d'outil :
    légitime (salutation, explication…), renvoyée telle quelle SANS relance."""
    from agent.agent_core import AgentCore

    llm = ScriptedLLM(["Bonjour ! Comment puis-je vous aider ?"])
    assert AgentCore(llm).run("salut") == "Bonjour ! Comment puis-je vous aider ?"
    assert len(llm.prompts) == 1


def test_loop_reprompts_when_tool_announced_without_any_json(sandbox_root, monkeypatch):
    """Régression « coupe du monde 2026 » (sans balises <think>) : le modèle
    ANNONCE l'outil (« je vais appeler calc… ») puis conclut sans avoir jamais
    émis de JSON. L'agent doit relancer avec une exigence de JSON strict au
    lieu d'accepter une réponse sortie de mémoire."""
    import tools.tool_registry as registry
    from agent.agent_core import AgentCore

    calls = []

    def fake_calc(expression):
        calls.append(expression)
        return 4

    monkeypatch.setitem(registry.TOOLS, "calc", fake_calc)
    monkeypatch.setitem(registry.REQUIRED_ARGS, "calc", ["expression"])

    llm = ScriptedLLM([
        "Je vais appeler calc sur 2+3 tout de suite.",
        '{"tool": "calc", "args": {"expression": "2+3"}}',
        "Le calcul donne bien quatre.",
    ])
    answer = AgentCore(llm).run("calcule 2+3")

    assert answer == "Le calcul donne bien quatre."
    assert calls == ["2+3"]  # l'outil a VRAIMENT été exécuté
    # La relance nomme l'outil annoncé et exige le JSON strict.
    nudge = llm.prompts[1]
    assert "« calc »" in nudge
    assert "AUCUN JSON" in nudge
    assert '{"tool": "calc"' in nudge


def test_loop_self_corrects_missing_arguments(sandbox_root):
    from agent.agent_core import AgentCore
    from tools.file_tools import write_file

    write_file("f.txt", "hello")
    llm = ScriptedLLM([
        '{"tool": "read_file", "args": {}}',
        '{"tool": "read_file", "args": {"path": "f.txt"}}',
        "Contenu affiché.",
    ])
    assert AgentCore(llm).run("lis f.txt") == (
        "Contenu affiché.\n\n"
        "[auto-correction] Arguments manquants pour read_file : ['path']. "
        "Reçu : {}. Format attendu : {\"tool\": \"read_file\", \"args\": {...}} — "
        "arguments obligatoires : ['path']. Renvoie UN SEUL JSON corrigé."
    )
    assert "Arguments manquants pour read_file : ['path']" in llm.prompts[1]


def test_gives_up_after_max_rounds_with_unknown_tool():
    """Budget de rounds épuisé sur des erreurs répétées : conclusion finale
    enrichie du dernier problème signalé (suffixe [auto-correction])."""
    from agent.agent_core import AgentCore

    llm = ScriptedLLM(['{"tool": "division", "args": {"a": 1}}'] * 20)
    answer = AgentCore(llm, max_rounds=4).run("divise")

    assert "Tool inconnu : 'division'" in answer
    assert "[auto-correction]" in answer
    assert len(llm.prompts) == 5  # 4 tours LLM + l'appel de conclusion finale


def test_conclusion_after_max_rounds_reports_problems():
    """Plafond atteint sur des erreurs répétées : le modèle reçoit bien une
    demande de bilan citant les problèmes accumulés, et la réponse rendue à
    l'utilisateur garde la trace transparente du suffixe [auto-correction]."""
    from agent.agent_core import AgentCore

    llm = ScriptedLLM(
        ['{"tool": "add", "args": {"a": 1}}'] * 3 + ["Voici mon bilan."]
    )
    answer = AgentCore(llm, max_rounds=3).run("calcule longuement")

    assert answer.startswith("Voici mon bilan.")
    assert "[auto-correction] Arguments manquants pour add : ['b']" in answer
    # Le prompt de conclusion annonce le plafond et rejoue les problèmes.
    conclusion_prompt = llm.prompts[3]
    assert "Nombre maximum d" in conclusion_prompt
    assert "Arguments manquants pour add : ['b']" in conclusion_prompt
    assert len(llm.prompts) == 4  # 3 tours LLM + l'appel de conclusion finale


def test_final_prompt_forbids_inventing_content(sandbox_root):
    """Le prompt post-outil impose de s'appuyer sur le résultat OBTENU."""
    from agent.agent_core import AgentCore

    llm = ScriptedLLM([
        '{"tool": "add", "args": {"a": 1, "b": 2}}',
        "1 + 2 = 3.",
    ])
    AgentCore(llm).run("calcule")
    final_prompt = llm.prompts[1]
    assert final_prompt.startswith("Dernier résultat : 3.")
    assert "en TEXTE NORMAL" in final_prompt
    assert "UN SEUL JSON" in final_prompt


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
    assert "- fake_tool_x(x) : Fait un truc factice" in prompt
