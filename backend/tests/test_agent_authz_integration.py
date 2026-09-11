# project/tests/test_agent_authz_integration.py
"""Intégration P2 Lot A : la boucle agentique respecte la PDP (flag / injection).

Couvre la couture ``app/agent/core.py`` :
    - flag ``security_authz_casbin`` désactivé (défaut) : zéro changement ;
    - enforcer injecté en mode strict : outil non déclaré REJETÉ et audité ;
    - enforcer injecté en mode legacy (shadow) : verdict historique conservé.
"""

from __future__ import annotations

from app.agent.core import AgentCore, RunStatus
from app.domain.entities.plan import Intent
from app.infrastructure.security.authz.casbin_pdp import build_pdp
from app.infrastructure.security.authz.enforcer import AuthzPolicyEnforcer
from app.infrastructure.security.authz.policy_document import load_policy_document
from app.infrastructure.security.authz.tool_capabilities import ToolCapabilityRegistry


class ScriptedLLM:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)

    def call(self, messages):
        return self.replies.pop(0)

    def call_stream(self, messages, on_thinking=None, on_content=None):
        return self.call(messages)


class FakeRegistry:
    def tool_names(self):
        return ["now", "read_file", "outil_bizarre"]

    def get(self, tool):
        table = {
            "now": lambda: "2026-09-11T12:00:00Z",
            "read_file": lambda path="": "contenu",
            "outil_bizarre": lambda **kw: "NE DOIT PAS ÊTRE EXÉCUTÉ",
        }
        return table.get(tool)

    def meta(self, tool):
        return {"description": f"outil {tool}", "required_args": []}


def intent() -> Intent:
    return Intent(prompt="question", session_id="s1", max_rounds=3)


def run_with_plan(core: AgentCore, plan_json: str):
    """Run complet avec un LLM scénarisé : plan JSON puis réponse finale."""
    core._llm = ScriptedLLM([plan_json, "FINAL : terminé"])
    return core.run(intent())


def make_enforcer(tenant_mode: str, tenant: str = "default") -> AuthzPolicyEnforcer:
    doc = load_policy_document(
        {
            "policy_version": 4,
            "default_tenant_mode": tenant_mode,
            "tenant_modes": {"default": tenant_mode},
            "roles": {
                "agent": [
                    "tool.execute:read",
                    "tool.execute:system",
                    "tool.execute:write",
                ]
            },
            "denies": [
                {
                    "role": "*",
                    "action": "tool.execute:unknown",
                    "resource": "*",
                    "reason": "capacité non déclarée (deny-by-default)",
                }
            ],
        }
    )
    return AuthzPolicyEnforcer(
        pdp=build_pdp(doc), policy=doc, capabilities=ToolCapabilityRegistry()
    )


# --- Enforcer injecté : strict rejette l'outil non déclaré --------------------


def test_strict_mode_rejects_undeclared_tool(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_AUTHZ_TENANT", "default")
    core = AgentCore(
        ScriptedLLM([]),
        FakeRegistry(),
        authz_enforcer=make_enforcer("strict"),
    )
    result = run_with_plan(
        core, '{"plan": [{"tool": "outil_bizarre", "args": {"x": "1"}}]}'
    )
    assert result.status is RunStatus.COMPLETED  # le LLM reformule puis conclut
    rejected = [t for t in result.actions if t.status == "rejected"]
    assert rejected, "l'outil non déclaré doit être rejeté par la PDP"
    trace = rejected[0]
    assert trace.decision == "reject"
    assert trace.policy_version == 4
    assert "non déclarée" in trace.authz_reason
    assert "non déclarée" in trace.error
    # L'outil n'a JAMAIS été exécuté : aucune trace done pour lui.
    assert all(
        not (t.tool == "outil_bizarre" and t.status == "done")
        for t in result.actions
    )


def test_strict_mode_allows_declared_read_tool(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_AUTHZ_TENANT", "default")
    core = AgentCore(
        ScriptedLLM([]), FakeRegistry(), authz_enforcer=make_enforcer("strict")
    )
    result = run_with_plan(core, '{"plan": [{"tool": "now", "args": {}}]}')
    done = [t for t in result.actions if t.status == "done"]
    assert done and done[0].tool == "now"


def test_shadow_mode_keeps_historical_verdicts(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_AUTHZ_TENANT", "default")
    core = AgentCore(
        ScriptedLLM([]), FakeRegistry(), authz_enforcer=make_enforcer("legacy_permissive")
    )
    # ``outil_bizarre`` : catégorie UNKNOWN -> verdict sandbox historique
    # APPROVE (validation humaine), PAS un rejet PDP (mode shadow).
    result = run_with_plan(
        core, '{"plan": [{"tool": "outil_bizarre", "args": {"x": "1"}}]}'
    )
    assert result.status is RunStatus.PENDING_APPROVAL
    assert all(t.status != "rejected" for t in result.actions)
    assert all(t.policy_version == 0 for t in result.actions)  # traces legacy


def test_sandbox_hard_rules_survive_strict_mode(monkeypatch) -> None:
    """Les règles dures sandbox (cibles sensibles) restent bloquantes : la PDP
    autorise la capacité (write déclarée) mais la sandbox refuse la cible
    ``.env`` — défense en profondeur, chaque couche bloque indépendamment."""
    monkeypatch.setenv("AGENT_AUTHZ_TENANT", "default")
    core = AgentCore(
        ScriptedLLM([]), FakeRegistry(), authz_enforcer=make_enforcer("strict")
    )
    result = run_with_plan(
        core,
        '{"plan": [{"tool": "write_file", "args": {"filename": ".env", "content": "x"}}]}',
    )
    rejected = [t for t in result.actions if t.status == "rejected"]
    assert rejected, "le chemin sensible doit rester rejeté (règle dure sandbox)"
    assert rejected[0].policy_version == 0  # rejet SANDBOX (règle dure), pas PDP
    assert "cible sensible" in rejected[0].error


def test_flag_off_keeps_legacy_behavior(monkeypatch) -> None:
    monkeypatch.delenv("AGENT_SECURITY_AUTHZ_CASBIN", raising=False)
    core = AgentCore(ScriptedLLM([]), FakeRegistry())
    assert core._resolve_authz_gate() is None


def test_flag_on_resolves_default_enforcer(monkeypatch) -> None:
    from app.infrastructure.security.authz import factory

    monkeypatch.setenv("AGENT_SECURITY_AUTHZ_CASBIN", "1")
    monkeypatch.setenv("AGENT_AUTHZ_TENANT", "default")
    factory.reset_default_enforcer()
    try:
        core = AgentCore(ScriptedLLM([]), FakeRegistry())
        gate = core._resolve_authz_gate()
        assert gate is not None
        assert gate.mode.value in ("legacy_permissive", "strict")
    finally:
        factory.reset_default_enforcer()
