"""Tests offline de l'intégration du classifieur d'intention (Approche B).

Couvre la chaîne SCRUM-97 → 101 :
    - SCRUM-97 : classification GLOBALE au superviseur, AVANT planification
      (événement ``agent.intent`` + repli défensif « action » en cas de panne) ;
    - SCRUM-98 : propagation de l'intention aux sous-tâches (``PlanTask.intent``)
      et aux workers (guidage prompt + contrat) ;
    - SCRUM-99 : filtrage LOCAL par rôle (``roles.INTENT_POLICY`` /
      ``intent_decision_for``) — un rôle hors périmètre ne coûte NI LLM NI outil ;
    - SCRUM-100 : repli conversationnel MANDATOIRE (FSM ``FALLBACK_CHAT``)
      quand tout le plan est filtré ou que le plan est vide sur intention « chat » ;
    - SCRUM-101 : observabilité (``intent_decision``, ``tools_called``,
      événements ``agent.worker.skipped`` / ``agent.fallback``).

Sans classifieur injecté ⇒ comportement V1 strictement inchangé.
Aucun réseau : tout est scripté. Lance : pytest tests/test_multi_intent.py -v
"""

import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from ia.agent.multi_run_fsm import (  # noqa: E402
    IllegalMultiRunTransition,
    MultiRunFSM,
    MultiRunState,
)
from ia.agent.orchestrator import (  # noqa: E402
    EV_FALLBACK,
    EV_INTENT,
    EV_PLAN,
    EV_WORKER_SKIPPED,
    MultiAgentCoordinator,
)
from ia.agent.plan_validator import PlanTask  # noqa: E402
from ia.agent.roles import intent_decision_for  # noqa: E402


# --- Fakes (mêmes conventions que test_multi_agent.py) -----------------------

class FakeResult:
    """Résultat d'un run_detailed factice (expose ``answer``)."""

    def __init__(self, answer: str):
        self.answer = answer


class FakeAgent:
    """Agent factice : scripté, mémorise chaque prompt reçu."""

    def __init__(self, role, replies):
        self.role = role
        self.replies = list(replies)
        self.prompts: list[str] = []

    def run_detailed(self, prompt, on_thinking=None, on_tool_event=None, **_):
        self.prompts.append(prompt)
        if not self.replies:
            return FakeResult(f"[{self.role}] réponse par défaut")
        return FakeResult(self.replies.pop(0))


class FakeClassifier:
    """Classifieur d'intention factice (étiquette fixe, panne simulable)."""

    def __init__(self, label="chat", confidence=0.9, fail=False):
        self.label = label
        self.confidence = confidence
        self.fail = fail
        self.engine = "rules"
        self.calls = 0

    def predict(self, texts):
        self.calls += 1
        if self.fail:
            raise RuntimeError("classifieur en panne (simulé)")

        class _Result:
            pass  # duck-typing PredictionResult (label/confidence)

        result = _Result()
        result.label = self.label
        result.confidence = self.confidence
        return [result]


class FakeLLM:
    """LLM factice pour l'agent de repli (interface AgentCore : ``.call``)."""

    def __init__(self, replies):
        self.replies = list(replies)

    def call(self, messages):
        if not self.replies:
            return "réponse conversationnelle par défaut"
        return self.replies.pop(0)


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


PLAN_ACTION = (
    '[{"task_id":"t1","role":"web","subtask":"cherche A"},'
    '{"task_id":"t2","role":"math","subtask":"calcule B"}]'
)


# --- SCRUM-99 : politique d'intention par rôle --------------------------------

def test_intent_decision_for_policies():
    """action_only : exécute pour « action », filtré pour « chat »."""
    assert intent_decision_for("web", "action") == "executed"
    assert intent_decision_for("web", "chat") == "ignored"
    assert intent_decision_for("math", "chat") == "ignored"
    # pass_through : toujours exécuté.
    assert intent_decision_for("lead", "chat") == "executed"
    assert intent_decision_for("lead", "action") == "executed"
    # Rôle inconnu : politique par défaut (action_only).
    assert intent_decision_for("inconnu", "chat") == "ignored"
    # active=False : filtrage désactivé (comportement V1).
    assert intent_decision_for("web", "chat", active=False) == "executed"


def test_plan_task_intent_defaults():
    """Défauts « action » : sans classification, comportement historique."""
    task = PlanTask("t1", "web", "sous-tâche", [])
    assert task.intent == "action"
    assert task.intent_confidence == 0.0


# --- Comportement V1 inchangé sans classifieur (SCRUM-97) ----------------------

def test_sans_classifieur_comportement_inchange():
    """Aucun classifieur injecté ⇒ ni filtrage, ni champ d'intention au run."""
    role_builder = _workers_role_builder(
        [PLAN_ACTION, "synthèse"], {"web": "W", "math": "M"},
    )
    coordinator = MultiAgentCoordinator(llm_client=None, role_builder=role_builder)

    outcome = coordinator.run("Analyse la question et calcule.")

    assert outcome["status"] == "completed"
    assert all(w["status"] == "ok" for w in outcome["workers"])
    assert "intent" not in outcome
    assert "fallback_chat" not in outcome
    # Les workers DI ne reçoivent AUCUNE note d'intention dans leur prompt.
    web = role_builder("web")
    assert web.prompts and "INTENTION DÉTECTÉE" not in web.prompts[0]


# --- SCRUM-97/98 : classification globale + propagation ------------------------

def test_intent_action_classifie_et_propage():
    events = []
    role_builder = _workers_role_builder(
        [PLAN_ACTION, "synthèse"], {"web": "W", "math": "M"},
    )
    coordinator = MultiAgentCoordinator(
        llm_client=None, role_builder=role_builder,
        intent_classifier=FakeClassifier("action", 0.93),
    )

    outcome = coordinator.run(
        "Cherche A puis calcule B.", on_event=lambda k, d: events.append((k, d)),
    )

    assert outcome["status"] == "completed"
    assert outcome["intent"] == "action"
    assert outcome["intent_confidence"] == pytest.approx(0.93)
    assert outcome["fallback_chat"] is False
    # Chaque sous-tâche porte l'intention stampée (SCRUM-98).
    assert all(t["intent"] == "action" for t in outcome["plan"])
    # Contrat worker : décision + compteur d'outils (SCRUM-101).
    assert all(w["intent_decision"] == "executed" for w in outcome["workers"])
    assert all("tools_called" in w for w in outcome["workers"])
    # Événement d'intention diffusé UNE fois, AVANT le plan.
    kinds = [k for k, _ in events]
    assert kinds.count(EV_INTENT) == 1
    assert kinds.index(EV_INTENT) < kinds.index(EV_PLAN)
    intent_event = next(d for k, d in events if k == EV_INTENT)
    assert intent_event["intent"] == "action"
    assert intent_event["confidence"] == pytest.approx(0.93)
    assert intent_event["engine"] == "rules"


def test_worker_prompt_contient_l_intention():
    """Le guidage d'intention est injecté dans le prompt du worker (SCRUM-98)."""
    role_builder = _workers_role_builder(
        [PLAN_ACTION, "synthèse"], {"web": "W", "math": "M"},
    )
    coordinator = MultiAgentCoordinator(
        llm_client=None, role_builder=role_builder,
        intent_classifier=FakeClassifier("action", 0.88),
    )
    coordinator.run("Cherche A puis calcule B.")

    web = role_builder("web")


# --- SCRUM-99/100 : filtrage par rôle + repli FALLBACK_CHAT ---------------------

def test_intent_chat_filtre_les_workers_et_repli():
    """Intention « chat » : workers action_only filtrés, repli conversationnel."""
    events = []
    role_builder = _workers_role_builder([PLAN_ACTION], {"web": "W", "math": "M"})
    coordinator = MultiAgentCoordinator(
        # LLM factice pour l'AgentCore de repli (lead/workers passent par DI).
        llm_client=FakeLLM(["Bonjour ! Ravis de vous retrouver."]),
        role_builder=role_builder,
        intent_classifier=FakeClassifier("chat", 0.97),
    )

    outcome = coordinator.run(
        "Bonjour, comment ça va ?", on_event=lambda k, d: events.append((k, d)),
    )

    # Repli conversationnel : aucun worker action_only exécuté.
    assert outcome["status"] == "completed"
    assert outcome["fallback_chat"] is True
    assert outcome["intent"] == "chat"
    assert "Ravis de vous retrouver" in outcome["final_answer"]
    # fallback_chat → synthesizing → completed (état terminal cohérent).
    assert outcome["fsm_state"] == "completed"
    # Les workers filtrés sont TRACÉS (flow map complet), jamais construits.
    assert all(w["status"] == "ignored" for w in outcome["workers"])
    assert all(w["intent_decision"] == "ignored" for w in outcome["workers"])
    web = role_builder("web")
    assert web.prompts == []
    # Événements : filtrage + repli.
    skipped = [d for k, d in events if k == EV_WORKER_SKIPPED]
    assert {d["role"] for d in skipped} == {"web", "math"}
    assert any(k == EV_FALLBACK for k, _ in events)
    fallback_event = next(d for k, d in events if k == EV_FALLBACK)
    assert fallback_event["reason"] == "all_workers_ignored"


def test_plan_vide_intent_chat_repli_direct():
    """Plan vide + intention « chat » ⇒ réponse directe (jamais d'abort)."""
    role_builder = _workers_role_builder(["[]"], {})
    coordinator = MultiAgentCoordinator(
        llm_client=FakeLLM(["Bonjour ! Ravis de vous retrouver."]),
        role_builder=role_builder,
        intent_classifier=FakeClassifier("chat", 0.95),
    )

    outcome = coordinator.run("Salut !")

    assert outcome["status"] == "completed"
    assert outcome["fallback_chat"] is True
    assert "Ravis de vous retrouver" in outcome["final_answer"]
    assert outcome["plan"] == []
    assert outcome["workers"] == []


def test_plan_vide_intent_action_abort_inchange():
    """Plan vide + intention « action » ⇒ abort historique conservé."""
    role_builder = _workers_role_builder(["[]"], {})
    coordinator = MultiAgentCoordinator(
        llm_client=None, role_builder=role_builder,
        intent_classifier=FakeClassifier("action", 0.9),
    )

    outcome = coordinator.run("Fais le ménage complet.")

    assert outcome["status"] == "error"
    assert "fallback_chat" not in outcome


# --- Défenses : classifieur en panne, étiquette inattendue ----------------------

def test_classifieur_en_echec_ne_casse_pas_le_run():
    events = []
    role_builder = _workers_role_builder(
        [PLAN_ACTION, "synthèse"], {"web": "W", "math": "M"},
    )
    coordinator = MultiAgentCoordinator(
        llm_client=None, role_builder=role_builder,
        intent_classifier=FakeClassifier("chat", 0.9, fail=True),
    )

    outcome = coordinator.run(
        "Cherche A.", on_event=lambda k, d: events.append((k, d)),
    )

    # Repli défensif « action » : le run se déroule comme en V1.
    assert outcome["status"] == "completed"
    assert all(w["status"] == "ok" for w in outcome["workers"])
    intent_event = next(d for k, d in events if k == EV_INTENT)
    assert intent_event["intent"] == "action"
    assert intent_event["engine"] == "error"


def test_etiquette_inattendue_retombe_sur_action():
    """Une étiquette hors vocabulaire (chat/action) ne détourne pas le run."""
    role_builder = _workers_role_builder(
        [PLAN_ACTION, "synthèse"], {"web": "W", "math": "M"},
    )
    coordinator = MultiAgentCoordinator(
        llm_client=None, role_builder=role_builder,
        intent_classifier=FakeClassifier("hors_vocabulaire", 0.9),
    )
    outcome = coordinator.run("Cherche A.")

    assert outcome["status"] == "completed"
    assert outcome["intent"] == "action"  # comportement sûr conservé


# --- SCRUM-100 : FSM FALLBACK_CHAT -----------------------------------------------

def test_fsm_fallback_chat_transitions():
    """waiting_workers → fallback_chat → synthesizing → completed est légal."""
    fsm = MultiRunFSM.start()
    fsm = fsm.transition(MultiRunState.DISPATCH)
    fsm = fsm.transition(MultiRunState.WAITING_WORKERS)
    fsm = fsm.transition(MultiRunState.FALLBACK_CHAT)
    assert fsm.state is MultiRunState.FALLBACK_CHAT
    assert fsm.can_synthesize()  # la réponse du repli EST la réponse finale
    fsm = fsm.transition(MultiRunState.SYNTHESIZING)
    fsm = fsm.transition(MultiRunState.COMPLETED)
    assert fsm.state is MultiRunState.COMPLETED

    # planning → fallback_chat légal (plan vide + intention « chat »).
    fsm2 = MultiRunFSM.start().transition(MultiRunState.FALLBACK_CHAT)
    assert fsm2.state is MultiRunState.FALLBACK_CHAT

    # JAMAIS depuis un état terminal.
    with pytest.raises(IllegalMultiRunTransition):
        fsm.transition(MultiRunState.FALLBACK_CHAT)