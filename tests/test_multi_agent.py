"""Tests offline de l'orchestrateur multi-agents (plan → dispatch → synthèse).

Le LLM réel est remplacé par des FAKES agents via le DI ``role_builder``
(signature callable ``(role_name) -> agent`` avec ``run_detailed``). On vérifie :
    - cycle superviseur : plan valide -> workers exécutés -> synthèse finale ;
    - isolation STRICTE du contexte : un worker ne voit que SA sous-tâche
      (+ le contexte global résumé), jamais les résultats des autres workers ;
    - séquentiel vs parallèle : résultats identiques (déterminisme) ;
    - gamme d'événements de streaming (plan / worker.start / worker.result /
      synthèse / done) ;
    - échec d'un worker -> continueBroken : statut error + bloc ``unexecuted``
      explicite, réponse finale quand même produite s'il reste au moins un ok ;
    - isolation des outils par rôle dans AgentCore (garde de rôle /
      ``on_tool_forbidden``).

Aucun réseau : tout est scripté. Lance : pytest tests/test_multi_agent.py -v
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from ia.agent.agent_core import AgentCore  # noqa: E402
from ia.agent.orchestrator import (  # noqa: E402
    EV_DONE,
    EV_PLAN,
    EV_SYNTHESIZING,
    EV_WORKER_RESULT,
    EV_WORKER_START,
    MultiAgentCoordinator,
    build_role_agent,
)

# --- Fakes -------------------------------------------------------------------

class FakeResult:
    """Résultat d'un run_detailed factice (expose ``answer``)."""

    def __init__(self, answer: str):
        self.answer = answer


class FakeAgent:
    """Agent factice : scripté, mémorise chaque prompt reçu, peut lancer une
    exception (un appel échoue) pour simuler une panne worker."""

    def __init__(self, role, replies, fail_on_call=None):
        self.role = role
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.fail_on_call = fail_on_call  # index (0-based) -> lève une erreur

    def run_detailed(self, prompt, on_thinking=None, on_tool_event=None, **_):
        self.prompts.append(prompt)
        if self.fail_on_call is not None and len(self.prompts) == self.fail_on_call + 1:
            raise RuntimeError("worker down (simulé)")
        if not self.replies:
            return FakeResult(f"[{self.role}] réponse par défaut")
        return FakeResult(self.replies.pop(0))


def _workers_role_builder(lead_replies, worker_results):
    """role_builder qui scripte le lead (plan puis synthèse) et renvoie des
    fake workers dont le résultat dépend du rôle."""
    built = {}

    def _builder(role):
        if role in built:
            return built[role]
        if role == "lead":
            agent = FakeAgent("lead", list(lead_replies))
        else:
            agent = FakeAgent(role, [worker_results.get(role, f"résultat {role}")])
        built[role] = agent
        return agent

    return _builder


# --- Orchestration complète --------------------------------------------------

def test_supervisor_plan_dispatch_synthesis():
    plan_json = (
        '[{"task_id":"t1","role":"web","subtask":"cherche A"},'
        '{"task_id":"t2","role":"math","subtask":"calcule B"}]'
    )
    role_builder = _workers_role_builder(
        [plan_json, "Réponse finale synthétisée."],
        {"web": "INFO-WEB", "math": "RESULTAT-MATH"},
    )
    coordinator = MultiAgentCoordinator(llm_client=None, role_builder=role_builder)

    outcome = coordinator.run("Analyse la question et calcule.")

    assert outcome["status"] == "completed"
    assert "Réponse finale synthétisée" in outcome["final_answer"]
    assert [w["role"] for w in outcome["workers"]] == ["web", "math"]
    assert outcome["workers"][0]["status"] == "ok"
    assert outcome["workers"][0]["result"] == "INFO-WEB"
    # Aucune sous-tâche non exécutée.
    assert outcome["unexecuted"] == []


def test_workers_see_strict_isolation_no_shared_results():
    """Isolation stricte : le prompt d'un worker ne contient JAMAIS le résultat
    d'un autre worker, seulement SA sous-tâche + le contexte résumé."""
    plan_json = (
        '[{"task_id":"t1","role":"web","subtask":"cherche A"},'
        '{"task_id":"t2","role":"math","subtask":"calcule B"}]'
    )
    role_builder = _workers_role_builder(
        [plan_json, "synthèse"],
        {"web": "INFO-WEB", "math": "RESULTAT-MATH"},
    )
    coordinator = MultiAgentCoordinator(llm_client=None, role_builder=role_builder)
    coordinator.run("Question globale.")

    agents = {"web": role_builder("web"), "math": role_builder("math")}
    web_prompt = agents["web"].prompts[0]
    math_prompt = agents["math"].prompts[0]
    # Chacun voit SA sous-tâche...
    assert "cherche A" in web_prompt
    assert "calcule B" in math_prompt
    # ... et AUCUN ne voit le résultat de l'autre.
    assert "INFO-WEB" not in math_prompt
    assert "RESULTAT-MATH" not in web_prompt
    # Le plan complet n'est pas injecté aux workers (juste leur sous-tâche).
    assert '{"task_id"' not in web_prompt


def test_parallel_produces_same_results():
    plan_json = (
        '[{"task_id":"t1","role":"web","subtask":"cherche A"},'
        '{"task_id":"t2","role":"math","subtask":"calcule B"}]'
    )
    seq = _workers_role_builder(
        [plan_json, "synthèse"], {"web": "W", "math": "M"}
    )
    par = _workers_role_builder(
        [plan_json, "synthèse"], {"web": "W", "math": "M"}
    )
    o_seq = MultiAgentCoordinator(llm_client=None, role_builder=seq, parallel=False)
    o_par = MultiAgentCoordinator(llm_client=None, role_builder=par, parallel=True, max_workers=2)

    r_seq = o_seq.run("q")
    r_par = o_par.run("q")

    assert [w["result"] for w in r_seq["workers"]] == ["W", "M"]
    assert [w["result"] for w in r_par["workers"]] == ["W", "M"]


def test_streaming_events_emitted():
    events = []
    plan_json = (
        '[{"task_id":"t1","role":"web","subtask":"cherche A"}]'
    )
    role_builder = _workers_role_builder([plan_json, "synthèse"], {"web": "INFO-WEB"})
    coordinator = MultiAgentCoordinator(llm_client=None, role_builder=role_builder)
    coordinator.run("q", on_event=lambda kind, data: events.append((kind, data)))

    kinds = [k for k, _ in events]
    assert EV_PLAN in kinds
    assert EV_WORKER_START in kinds
    assert EV_WORKER_RESULT in kinds
    assert EV_SYNTHESIZING in kinds
    assert EV_DONE in kinds


def test_worker_failure_continue_broken():
    """Un worker en échec => continueBroken : statut error + ``unexecuted``
    explicite, mais synthèse produite depuis le worker réussi."""
    plan_json = (
        '[{"task_id":"t1","role":"web","subtask":"cherche A"},'
        '{"task_id":"t2","role":"math","subtask":"calcule B"}]'
    )
    built = {}

    def role_builder(role):
        if role in built:
            return built[role]
        if role == "lead":
            a = FakeAgent("lead", [plan_json, "synthèse partielle"])
        elif role == "web":
            a = FakeAgent("web", [], fail_on_call=0)  # échoue au 1er appel
        else:
            a = FakeAgent(role, ["RESULTAT-MATH"])
        built[role] = a
        return a

    coordinator = MultiAgentCoordinator(llm_client=None, role_builder=role_builder)
    outcome = coordinator.run("q")

    assert outcome["status"] == "completed"
    assert "synthèse partielle" in outcome["final_answer"]
    by_role = {w["role"]: w for w in outcome["workers"]}
    assert by_role["web"]["status"] == "error"
    assert by_role["math"]["status"] == "ok"
    assert len(outcome["unexecuted"]) == 1
    assert outcome["unexecuted"][0]["role"] == "web"
    assert outcome["unexecuted"][0]["error_code"]


def test_invalid_plan_aborts_global():
    """Plan non exploitable => abort global, aucune exécution de worker."""
    built = {}

    def role_builder(role):
        if role not in built:
            a = FakeAgent(role, ["Pas de JSON valide ici."] if role == "lead" else ["ignoré"])
            built[role] = a
        return built[role]

    coordinator = MultiAgentCoordinator(llm_client=None, role_builder=role_builder)
    outcome = coordinator.run("q")

    assert outcome["status"] == "error"
    assert outcome["final_answer"] == ""
    assert outcome["workers"] == []
# --- Isolation des outils par rôle (AgentCore) --------------------------------

def test_agentcore_role_gate_rejects_out_of_scope_tool():
    """La garde de rôle refuse un outil hors du sous-ensemble injecté et appelle
    on_tool_forbidden. Aucun hook quand l'outil est dans le périmètre."""
    calls = []
    tools = {"math_tool": lambda **k: "ok"}
    agent = AgentCore(
        llm_client=None,
        system_prompt="system",
        tools=tools,
        required_args={"math_tool": []},
        on_tool_forbidden=lambda tool, msg: calls.append((tool, msg)),
    )
    # Outil hors périmètre -> refus + hook.
    assert agent._gate_role("shell.exec") is not None
    assert calls and calls[-1][0] == "shell.exec"
    assert "Outil interdit" in calls[-1][1]
    # Outil du rôle -> pas de refus.
    assert agent._gate_role("math_tool") is None


def test_build_role_agent_isolates_tools():
    """build_role_agent('web', …) produit un AgentCore dont le sous-ensemble
    d'outils est strictement celui du rôle web (web_search présent, pas
    shell.exec)."""
    registry = {"web_search": lambda **k: "ok", "shell.exec": lambda **k: "ok"}
    required = {"web_search": ["q"], "shell.exec": []}
    agent = build_role_agent(
        "web", llm_client=None, tools_registry=registry, required_args_registry=required
    )
    assert "web_search" in agent._tools
    assert "shell.exec" not in agent._tools
    # La garde de rôle du worker web bloque un outil ops.
    assert agent._gate_role("shell.exec") is not None
    assert agent._gate_role("web_search") is None


def test_validate_plan_roundtrip():
    """Le plan produit par le lead (fake) passe par le validateur réel avant
    dispatch — vérifie l'intégration plan_validator / orchestrator."""
    plan_json = '[{"task_id":"t1","role":"web","subtask":"cherche A"}]'
    role_builder = _workers_role_builder([plan_json, "synthèse"], {"web": "W"})
    coordinator = MultiAgentCoordinator(
        llm_client=None, role_builder=role_builder, roles=["web", "math"]
    )
    outcome = coordinator.run("q")
    assert outcome["status"] == "completed"
    assert outcome["plan"][0]["role"] == "web"

# --- Reproduction du crash « bonjour » (alternance stricte des rôles) --------

class _CapturingLLM:
    """LLM factice qui scripte les réponses et MÉMORISE chaque liste de
    messages reçue — pour vérifier l'alternance des rôles réellement envoyée
    au serveur (contrat des templates Jinja Ollama/Mistral)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def call(self, messages):
        self.calls.append([dict(m) for m in messages])
        return self.replies.pop(0) if self.replies else "réponse par défaut"


def _assert_strict_alternation(messages):
    """Un seul « system » en tête, puis alternance stricte user/assistant."""
    roles = [m["role"] for m in messages]
    assert roles.count("system") == 1, f"messages system multiples : {roles}"
    assert roles[0] == "system", f"premier rôle ≠ system : {roles}"
    for prev, cur in zip(roles, roles[1:]):
        assert prev != cur, f"rôles consécutifs identiques ({prev}) : {roles}"


def test_bonjour_builds_strictly_alternating_messages():
    """REPRODUCTION du crash « bonjour » : le noyau envoyait DEUX messages
    « system » consécutifs (system prompt + contexte Edge) — les templates
    Jinja des serveurs Ollama/Mistral exigent une alternance stricte des rôles
    (TemplateError « Conversation roles must alternate user/assistant/... »)."""
    llm = _CapturingLLM(["Bonjour ! Comment puis-je vous aider ?"])
    agent = AgentCore(llm_client=llm, system_prompt="Tu es un assistant.")
    result = agent.run_detailed("bonjour")

    assert result.answer == "Bonjour ! Comment puis-je vous aider ?"
    assert len(llm.calls) == 1
    _assert_strict_alternation(llm.calls[0])
    # Le prompt utilisateur arrive intact en dernier message.
    assert llm.calls[0][-1]["role"] == "user"
    assert llm.calls[0][-1]["content"] == "bonjour"


def test_history_consecutive_same_roles_are_merged():
    """L'historique de session ne doit JAMAIS introduire deux messages
    consécutifs de même rôle (même TemplateError à la clé). Les tours
    adjacents de même rôle sont FONDUS (contenu préservé), et un historique
    se terminant par « user » est fondu dans le prompt courant."""
    llm = _CapturingLLM(["ok"])
    agent = AgentCore(llm_client=llm, system_prompt="system")
    agent.run_detailed("question", history_messages=[
        {"role": "user", "content": "u1"},
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "u3"},
    ])

    roles = [m["role"] for m in llm.calls[0]]
    assert roles == ["system", "user", "assistant", "user"]
    # u1 + u2 fusionnés dans un seul tour user (contenu préservé).
    assert "u1" in llm.calls[0][1]["content"] and "u2" in llm.calls[0][1]["content"]
    # u3 (dernier tour user de l'historique) fondu dans le prompt courant.
    assert "u3" in llm.calls[0][3]["content"] and "question" in llm.calls[0][3]["content"]


def test_coordinator_bonjour_end_to_end_roles_alternate():
    """BOUT EN BOUT « bonjour » via l'orchestrateur : le lead RÉEL
    (build_role_agent → AgentCore) reçoit des messages strictement alternés —
    plus aucun TemplateError côté serveur sur un simple message de chat."""
    llm = _CapturingLLM([
        '[{"task_id":"t1","role":"web","subtask":"salue"}]',  # plan du lead
        "Bonjour via web.",                                   # worker web
        "Bonjour ! (synthèse)",                               # synthèse lead
    ])
    built = {}

    def role_builder(role):
        if role not in built:
            built[role] = build_role_agent(
                role, llm_client=llm, tools_registry={}, required_args_registry={},
            )
        return built[role]

    coordinator = MultiAgentCoordinator(
        llm_client=None, role_builder=role_builder, parallel=False,
    )
    outcome = coordinator.run("bonjour")

    assert outcome["status"] == "completed"
    assert "synthèse" in outcome["final_answer"]
    assert llm.calls, "le LLM doit avoir été appelé"
    for messages in llm.calls:
        _assert_strict_alternation(messages)
    # Le prompt « bonjour » est bien arrivé au lead (dernier message user).
    assert "bonjour" in llm.calls[0][-1]["content"]
    assert llm.calls[0][-1]["role"] == "user"

