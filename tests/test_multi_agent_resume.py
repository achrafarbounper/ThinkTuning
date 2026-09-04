"""Tests de la REPRISE NATIVE multi-agents (faiblesses #2, #4, #5, #6).

Couvre ``MultiAgentCoordinator.run(..., resume_request_id=...)`` :
    - la reprise NE re-planifie PAS (rounds LLM minimaux) ;
    - seul le worker bloqué est re-dispatché, AVEC ``resume_request_id`` —
      l'action approuvée est rejouée DANS le même worker (jamais mono-agent) ;
    - la synthèse finale intègre le résultat repris + les workers déjà ok ;
    - les gardes : demande inconnue → erreur claire ; demande non approuvée
      → erreur (empreinte SHA-256 / statut exigés) ;
    - la réflexion du worker est diffusée (``agent.worker.thinking``) et
      agrégée dans le contrat de sortie.

Aucun réseau : tout est scripté. Lance : pytest tests/test_multi_agent_resume.py -v
"""

import pytest

from ia.agent.orchestrator import (
    EV_RESUMING,
    EV_WORKER_THINKING,
    MultiAgentCoordinator,
)


# --- Fakes --------------------------------------------------------------------

class FakeResult:
    """Résultat d'un run_detailed factice (``answer`` + ``thinking``)."""

    def __init__(self, answer: str, thinking: str = ""):
        self.answer = answer
        self.thinking = thinking


class LeadAgent:
    """Lead factice : scripte plan puis synthèse ; COMPTE plan vs synthèse.

    Le même agent « lead » sert au planning ET à la synthèse (comportement de
    l'orchestrateur : ``_build_lead`` est mémoïsé). Les compteurs séparés
    permettent de prouver qu'une reprise NE relance PAS le planning.
    """

    def __init__(self, plan_json: str, synthesis: str = "SYNTHÈSE FINALE"):
        self.plan_json = plan_json
        self.synthesis = synthesis
        self.calls = 0
        self.plan_calls = 0
        self.synthesis_calls = 0
        self.prompts: list[str] = []

    def run_detailed(self, prompt, on_thinking=None, on_tool_event=None, **_):
        self.calls += 1
        self.prompts.append(prompt)
        if "Décompose la demande" in prompt:  # marqueur du prompt PLANNER
            self.plan_calls += 1
            return FakeResult(self.plan_json)
        self.synthesis_calls += 1
        return FakeResult(self.synthesis)


class OkAgent:
    """Worker factice qui réussit immédiatement."""

    def __init__(self, role: str, answer: str):
        self.role = role
        self.answer = answer
        self.prompts: list[str] = []

    def run_detailed(self, prompt, on_thinking=None, resume_request_id=None, **_):
        assert resume_request_id is None, "un worker non bloqué ne reçoit JAMAIS de reprise"
        self.prompts.append(prompt)
        return FakeResult(self.answer)


class BlockingAgent:
    """Worker factice qui bloque sur un approve au 1er tour.

    À la reprise (``resume_request_id``), il rejoue l'action approuvée DANS le
    même agent et conclut (comportement miroir d'``AgentCore.run_detailed``).
    """

    def __init__(self, role: str, request_id: str = "req-1"):
        self.role = role
        self.request_id = request_id
        self.awaiting_request_id = None
        self.last_approval = None
        self.resume_ids: list[str] = []
        self.prompts: list[str] = []
        self.block_on_resume = False  # reprise qui redemande une validation
        self.next_request_id = "req-2"

    def run_detailed(self, prompt, on_thinking=None, resume_request_id=None,
                     on_tool_event=None, **_):
        self.prompts.append(prompt)
        if on_thinking is not None:
            on_thinking(f"réflexion de {self.role}")
        if resume_request_id:
            self.resume_ids.append(resume_request_id)
            if self.block_on_resume:
                self.block_on_resume = False  # une seule re-demande de validation
                self.awaiting_request_id = self.next_request_id
                return FakeResult("[En attente de validation] (action suivante)")
            self.awaiting_request_id = None
            return FakeResult(
                "action approuvée rejouée, conclusion", thinking="réflexion reprise"
            )
        self.awaiting_request_id = self.request_id
        return FakeResult("[En attente de validation]")


class _FakeApprovalStore:
    """Store d'approbation factice (statuts injectables)."""

    def __init__(self, rows: dict):
        self.rows = rows

    def get(self, request_id):
        return self.rows.get(request_id)


@pytest.fixture()
def approval_store(monkeypatch):
    """Monkeypatche ``core.approval_store.get_approval_store`` (lazy import)."""
    store = _FakeApprovalStore({
        "req-1": {"status": "approved", "tool": "write_file", "args_hash": "h1"},
        "req-2": {"status": "approved", "tool": "write_file", "args_hash": "h2"},
    })
    from core import approval_store as approval_store_module
    monkeypatch.setattr(approval_store_module, "get_approval_store", lambda: store)
    return store



# --- Cycle complet avec reprise -------------------------------------------------

def test_full_resume_cycle(approval_store):
    blocker = BlockingAgent("web", request_id="req-1")
    coordinator, lead = _coordinator(PLAN, blocking_role="web", blocker=blocker)

    # 1er run : le worker web bloque → awaiting_approval (AUCUNE synthèse).
    first = coordinator.run("Question globale.")
    assert first["status"] == "awaiting_approval"
    assert first["fsm_state"] == "awaiting_approval"
    assert lead.calls == 1
    assert [w["status"] for w in first["workers"]] == ["awaiting_approval", "ok"]
    assert first["pending_approvals"][0]["request_id"] == "req-1"

    # Reprise : PAS de re-planification, re-dispatch ciblé, synthèse finale.
    outcome = coordinator.run("Question globale.", resume_request_id="req-1")

    assert lead.plan_calls == 1, "la reprise NE doit PAS relancer le planning"
    assert lead.synthesis_calls == 1, "la synthèse finale est produite après reprise"
    assert blocker.resume_ids == ["req-1"], "l'action est rejouée dans le MÊME worker"
    assert outcome["status"] == "completed"
    assert outcome["fsm_state"] == "completed"
    assert outcome["final_answer"] == "SYNTHÈSE FINALE"
    by_id = {w["task_id"]: w for w in outcome["workers"]}
    assert by_id["t1"]["status"] == "ok"
    assert by_id["t1"]["resumed"] is True
    assert "action approuvée rejouée" in by_id["t1"]["result"]
    assert by_id["t2"]["status"] == "ok" and by_id["t2"]["resumed"] is False
    assert outcome["unexecuted"] == []
    assert "réflexion reprise" in outcome["thinking"]


def test_resume_event_is_emitted(approval_store):
    blocker = BlockingAgent("web", request_id="req-1")
    coordinator, _ = _coordinator(PLAN, blocking_role="web", blocker=blocker)
    coordinator.run("Question globale.")

    events: list[tuple] = []
    coordinator.run("Question globale.", resume_request_id="req-1",
                    on_event=lambda kind, data: events.append((kind, data)))
    kinds = [k for k, _ in events]
    assert EV_RESUMING in kinds
    resuming = next(data for kind, data in events if kind == EV_RESUMING)
    assert resuming["request_id"] == "req-1"
    assert resuming["task_id"] == "t1"
    assert kinds[-1] == "agent.done"


def test_worker_thinking_event_is_emitted(approval_store):
    """Faiblesse #4 : la réflexion du worker est diffusée en temps réel."""
    blocker = BlockingAgent("web", request_id="req-1")
    coordinator, _ = _coordinator(PLAN, blocking_role="web", blocker=blocker)

    events: list[tuple] = []
    coordinator.run("Question globale.",
                    on_event=lambda kind, data: events.append((kind, data)))
    thinking_events = [data for kind, data in events if kind == EV_WORKER_THINKING]
    assert thinking_events
    assert thinking_events[0]["task_id"] == "t1"
    assert thinking_events[0]["role"] == "web"
    assert "réflexion de web" in thinking_events[0]["thinking"]


def test_resume_with_new_approval_request(approval_store):
    """La reprise qui déclenche une NOUVELLE validation retourne en awaiting."""
    blocker = BlockingAgent("web", request_id="req-1")
    blocker.block_on_resume = True
    coordinator, lead = _coordinator(PLAN, blocking_role="web", blocker=blocker)
    coordinator.run("Question globale.")

    outcome = coordinator.run("Question globale.", resume_request_id="req-1")

    assert outcome["status"] == "awaiting_approval"
    assert outcome["fsm_state"] == "awaiting_approval"
    assert outcome["pending_approvals"][0]["request_id"] == "req-2"
    assert lead.plan_calls == 1 and lead.synthesis_calls == 0
    # La chaîne de reprise peut continuer (req-2 enregistrée + approuvée).
    final = coordinator.run("Question globale.", resume_request_id="req-2")
    assert final["status"] == "completed"
    assert blocker.resume_ids == ["req-1", "req-2"]


# --- Gardes de reprise -----------------------------------------------------------

def test_resume_unknown_request_id_is_a_clean_error():
    blocker = BlockingAgent("web", request_id="req-1")
    coordinator, lead = _coordinator(PLAN, blocking_role="web", blocker=blocker)
    coordinator.run("Question globale.")

    outcome = coordinator.run("Question globale.", resume_request_id="inconnu")

    assert outcome["status"] == "error"
    assert "Reprise impossible" in outcome["message"]
    assert lead.plan_calls == 1  # aucune re-planification déguisée


def test_resume_without_prior_run_is_a_clean_error(approval_store):
    coordinator, _ = _coordinator(PLAN)
    outcome = coordinator.run("Question globale.", resume_request_id="req-1")
    assert outcome["status"] == "error"
    assert "Reprise impossible" in outcome["message"]


def test_resume_requires_approved_request(monkeypatch):
    """Faiblesse #6 : la reprise exige une validation humaine (empreinte)."""
    store = _FakeApprovalStore({"req-1": {"status": "pending", "args_hash": "h1"}})
    from core import approval_store as approval_store_module
    monkeypatch.setattr(approval_store_module, "get_approval_store", lambda: store)

    blocker = BlockingAgent("web", request_id="req-1")
    coordinator, _ = _coordinator(PLAN, blocking_role="web", blocker=blocker)
    coordinator.run("Question globale.")

    outcome = coordinator.run("Question globale.", resume_request_id="req-1")
    assert outcome["status"] == "error"
    assert "non approuvée" in outcome["message"]
    assert blocker.resume_ids == [], "aucun rejeu sans approbation"
def _coordinator(plan_json, blocking_role="web", blocker=None):
    lead = LeadAgent(plan_json)
    others: dict[str, OkAgent] = {}

    def _builder(role):
        if role == "lead":
            return lead
        if role == blocking_role:
            return blocker
        if role not in others:
            others[role] = OkAgent(role, f"résultat {role}")
        return others[role]

    return MultiAgentCoordinator(llm_client=None, role_builder=_builder), lead


PLAN = (
    '[{"task_id":"t1","role":"web","subtask":"cherche A"},'
    '{"task_id":"t2","role":"math","subtask":"calcule B"}]'
)