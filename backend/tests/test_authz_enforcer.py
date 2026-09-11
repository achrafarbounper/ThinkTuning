# project/tests/test_authz_enforcer.py
"""Tests de l'enforcer d'autorisation (P2 Lot A) — modes de tenant, shadow, audit."""

from __future__ import annotations

from typing import Any

import pytest

from app.infrastructure.security.authz.casbin_pdp import build_pdp
from app.infrastructure.security.authz.enforcer import AuthzPolicyEnforcer
from app.infrastructure.security.authz.policy_document import load_policy_document
from app.infrastructure.security.authz.tool_capabilities import ToolCapabilityRegistry


class RecordingBus:
    """Event bus fake : enregistre les événements émis (audit)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def on(self, event_type, handler) -> None: ...

    def once(self, event_type, handler) -> None: ...

    def off(self, event_type, handler) -> None: ...

    def emit(self, event_type, **kwargs) -> None:
        self.events.append((event_type, kwargs))

    def emit_async(self, event_type, **kwargs) -> None: ...

    def clear(self, event_type=None) -> None: ...

    def listener_count(self, event_type) -> int:
        return 0


def policy_dict(version: int = 7, **overrides) -> dict:
    base: dict = {
        "policy_version": version,
        "default_tenant_mode": "legacy_permissive",
        "tenant_modes": {"default": "legacy_permissive", "prod": "strict"},
        "roles": {"agent": ["tool.execute:read", "tool.execute:system"]},
        "denies": [
            {
                "role": "*",
                "action": "tool.execute:unknown",
                "resource": "*",
                "reason": "capacité non déclarée (deny-by-default)",
            }
        ],
    }
    base.update(overrides)
    return base


def make_enforcer(
    *, policy: dict | None = None, bus: RecordingBus | None = None
) -> AuthzPolicyEnforcer:
    document = load_policy_document(policy if policy is not None else policy_dict())
    return AuthzPolicyEnforcer(
        pdp=build_pdp(document),
        policy=document,
        capabilities=ToolCapabilityRegistry(),
        event_bus=bus,
    )

# --- Modes de tenant -------------------------------------------------------


def test_legacy_permissive_is_shadow_not_enforcing() -> None:
    bus = RecordingBus()
    enforcer = make_enforcer(bus=bus)
    decision = enforcer.authorize_tool(
        tool="write_file", subject="agent", tenant="default", args_hash="h1"
    )
    assert decision.enforced is False  # verdict historique conservé
    assert bus.events, "l'audit shadow DOIT être émis même sans contrainte"
    name, payload = bus.events[0]
    assert name == "agent.authz_decision"
    assert payload["mode"] == "legacy_permissive"
    assert payload["policy_version"] == 7
    assert payload["tool"] == "write_file"
    # Aucune valeur d'argument dans l'audit (seulement le hash).
    assert "args" not in payload and payload["args_hash"] == "h1"


def test_strict_tenant_is_enforcing() -> None:
    enforcer = make_enforcer()
    # lecture déclarée, rôle agent autorisé : décision contraignante ALLOW
    decision = enforcer.authorize_tool(
        tool="read_file", subject="agent", tenant="prod", args_hash="h2"
    )
    assert decision.enforced is True
    assert decision.allowed is True  # read_file est déclaré (READ)
    from app.domain.authorization import AuthzEffect

    assert decision.effect is AuthzEffect.ALLOW


def test_strict_tenant_denies_undeclared_tool() -> None:
    enforcer = make_enforcer()
    decision = enforcer.authorize_tool(
        tool="echo", subject="agent", tenant="prod", args_hash="h3"
    )
    assert decision.enforced is True and decision.allowed is False
    assert decision.rule.startswith("deny:")
    assert "non déclarée" in decision.reason


def test_strict_tenant_denies_role_without_grant() -> None:
    enforcer = make_enforcer()
    decision = enforcer.authorize_tool(
        tool="write_file", subject="viewer", tenant="prod", args_hash=""
    )
    assert decision.allowed is False and decision.enforced is True
    assert decision.rule == "default_deny"


def test_unknown_tenant_falls_back_to_document_default() -> None:
    enforcer = make_enforcer()
    assert enforcer.tenant_mode("inconnu").value == "legacy_permissive"
    strict = make_enforcer(policy=policy_dict(default_tenant_mode="strict", tenant_modes={}))
    assert strict.tenant_mode("inconnu").value == "strict"


# --- Gate par run (boucle agent) --------------------------------------------


def test_run_gate_returns_none_for_shadow_and_allow() -> None:
    from app.domain.entities.plan import Action, ActionCategory

    enforcer = make_enforcer()
    gate = enforcer.gate_for_run(subject="agent", tenant="default")
    read_action = Action(tool="read_file", args={"path": "x"}, category=ActionCategory.READ)
    assert gate.check(read_action) is None  # shadow : sandbox décide
    prod_gate = enforcer.gate_for_run(subject="agent", tenant="prod")
    assert prod_gate.check(read_action) is None  # strict + allow : sandbox décide


def test_run_gate_rejects_undeclared_tool_in_strict() -> None:
    from app.domain.entities.plan import Action

    enforcer = make_enforcer()
    gate = enforcer.gate_for_run(subject="agent", tenant="prod")
    weird = Action(tool="outil_bizarre", args={"x": "1"})
    decision = gate.check(weird)
    assert decision is not None
    assert decision.allowed is False and decision.enforced is True


def test_gate_uses_canonical_role_alias() -> None:
    from app.domain.entities.plan import Action, ActionCategory

    doc = load_policy_document(
        policy_dict(role_aliases={"read_only": "agent"}, tenant_modes={"prod": "strict"})
    )
    enforcer = AuthzPolicyEnforcer(
        pdp=build_pdp(doc), policy=doc, capabilities=ToolCapabilityRegistry()
    )
    gate = enforcer.gate_for_run(subject="read_only", tenant="prod")
    read_action = Action(tool="read_file", args={}, category=ActionCategory.READ)
    assert gate.check(read_action) is None  # alias -> agent -> tool.execute:read


@pytest.mark.parametrize("missing", ["pdp", "policy"])
def test_enforcer_requires_pdp_and_policy(missing: str) -> None:
    from app.domain.errors import PolicyUnavailableError

    kwargs: dict = {
        "pdp": build_pdp(load_policy_document(policy_dict())),
        "policy": load_policy_document(policy_dict()),
    }
    kwargs[missing] = None
    with pytest.raises(PolicyUnavailableError):
        AuthzPolicyEnforcer(**kwargs)

