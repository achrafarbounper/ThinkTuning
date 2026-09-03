# project/tests/test_budget_pool.py
"""Tests du budget hiérarchique multi-agents (roadmap ⚙️#5).

Vérifie, hors réseau :
    - ``BudgetPool`` : réservation ferme, épuisement, release borné,
      thread-safety (dispatch parallèle) ;
    - intégration orchestrateur : workers refusés AVANT tout appel LLM quand
      le pool global est épuisé (``TokenBudgetExceeded``), snapshot ``budget``
      exposé dans le contrat de sortie, comportement V1 inchangé sans pool ;
    - propagation du quota réservé vers ``AgentCore.max_rounds``.

Lance : pytest tests/test_budget_pool.py -v
"""

from __future__ import annotations

import threading

from ia.agent.agent_core import AgentCore
from ia.agent.budget_pool import BudgetPool, build_budget_pool
from ia.agent.errors import TOKEN_BUDGET_EXCEEDED
from ia.agent.orchestrator import MultiAgentCoordinator, build_role_agent

# --- Fakes (mêmes conventions que test_multi_agent.py) -----------------------


class FakeResult:
    def __init__(self, answer: str):
        self.answer = answer


class FakeAgent:
    def __init__(self, role, replies):
        self.role = role
        self.replies = list(replies)

    def run_detailed(self, prompt, on_thinking=None, on_tool_event=None, **_):
        if not self.replies:
            return FakeResult(f"[{self.role}] défaut")
        return FakeResult(self.replies.pop(0))


def _role_builder(lead_replies, worker_results):
    built = {}

    def _builder(role):
        if role in built:
            return built[role]
        replies = list(lead_replies) if role == "lead" else [
            worker_results.get(role, f"résultat {role}")
        ]
        built[role] = FakeAgent(role, replies)
        return built[role]

    return _builder


_PLAN_3 = (
    '[{"task_id":"t1","role":"web","subtask":"cherche A"},'
    '{"task_id":"t2","role":"math","subtask":"calcule B"},'
    '{"task_id":"t3","role":"data","subtask":"query C"}]'
)


# --- BudgetPool isolé ---------------------------------------------------------


def test_pool_reserve_until_exhaustion():
    pool = BudgetPool(total=5)
    assert pool.reserve(3) == 3
    assert pool.reserve(3) == 2          # partiel : le reste disponible
    assert pool.reserve(1) == 0          # épuisé
    pool.release(1)
    assert pool.reserve(1) == 1


def test_pool_release_is_bounded_by_total():
    pool = BudgetPool(total=4)
    pool.release(10)                     # sans réservation préalable
    assert pool.snapshot()["available"] == 4
    pool.reserve(2)
    pool.release(99)
    assert pool.snapshot()["available"] == 4


def test_pool_rejects_non_positive_total():
    try:
        BudgetPool(total=0)
    except ValueError:
        pass
    else:
        raise AssertionError("BudgetPool(total=0) doit lever ValueError")


def test_pool_is_thread_safe():
    pool = BudgetPool(total=30)
    granted_total: list[int] = []
    lock = threading.Lock()

    def worker():
        for _ in range(10):
            granted = pool.reserve(3)
            with lock:
                granted_total.append(granted)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = pool.snapshot()
    assert snap["consumed"] == 30                    # jamais dépassé
    assert sum(granted_total) == 30
    assert snap["available"] == 0


def test_build_budget_pool_factory():
    assert build_budget_pool(None, per_worker_quota=6) is None
    assert build_budget_pool(0, per_worker_quota=6) is None
    pool = build_budget_pool(10, per_worker_quota=6)
    assert isinstance(pool, BudgetPool)
    assert pool.total == 10


# --- Intégration orchestrateur -------------------------------------------------


def _coordinator(max_total_tool_calls=None, max_worker_rounds=2, lead_replies=None):
    return MultiAgentCoordinator(
        llm_client=None,
        role_builder=_role_builder(
            lead_replies or [_PLAN_3, "synthèse finale."],
            {"web": "W", "math": "M", "data": "D"},
        ),
        max_worker_rounds=max_worker_rounds,
        max_total_tool_calls=max_total_tool_calls,
    )


def test_coordinator_without_pool_keeps_v1_behavior():
    outcome = _coordinator().run("Question globale.")
    assert outcome["status"] == "completed"
    assert [w["status"] for w in outcome["workers"]] == ["ok", "ok", "ok"]
    assert "budget" not in outcome                     # contrat V1 inchangé


def test_coordinator_pool_denies_workers_beyond_global_quota():
    """3 workers, quota global 4, quota/worker 2 => 2 admis, le 3e refusé
    AVANT tout appel LLM (TokenBudgetExceeded)."""
    outcome = _coordinator(max_total_tool_calls=4).run("Question globale.")
    denied = [w for w in outcome["workers"] if w["status"] == "error"]
    assert [w["status"] for w in outcome["workers"]].count("ok") == 2
    assert len(denied) == 1
    assert denied[0]["error_code"] == TOKEN_BUDGET_EXCEEDED
    assert "Budget global" in denied[0]["message"]
    snap = outcome["budget"]
    assert snap["workers_admitted"] == 2
    assert snap["available"] == 0 and snap["consumed"] == 4


def test_coordinator_pool_snapshot_in_complete_path():
    plan_json = (
        '[{"task_id":"t1","role":"web","subtask":"cherche A"},'
        '{"task_id":"t2","role":"math","subtask":"calcule B"}]'
    )
    outcome = MultiAgentCoordinator(
        llm_client=None,
        role_builder=_role_builder([plan_json, "synthèse."], {"web": "W", "math": "M"}),
        max_worker_rounds=3,
        max_total_tool_calls=10,
    ).run("Question globale.")
    assert outcome["status"] == "completed"
    assert outcome["budget"]["workers_admitted"] == 2


def test_reserved_quota_propagates_to_agent_core_max_rounds():
    """Le quota réservé borne les rounds LLM du worker (AgentCore.max_rounds)."""
    agent = build_role_agent("web", llm_client=object(), tools_registry={},
                             required_args_registry={}, max_rounds=3)
    assert isinstance(agent, AgentCore)
    assert agent.max_rounds == 3


def test_lead_has_no_tools():
    agent = build_role_agent("lead", llm_client=object(), tools_registry={},
                             required_args_registry={}, max_rounds=3)
    assert agent._tools == {}                           # lead sans outil

