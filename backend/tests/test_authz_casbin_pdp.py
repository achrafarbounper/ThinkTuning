# project/tests/test_authz_casbin_pdp.py
"""Tests de la PDP Casbin embarquée (P2 Lot A) — matrice, fail-closed, p95 < 5 ms."""

from __future__ import annotations

import time

import pytest

from app.domain.authorization import AuthzRequest
from app.domain.ports.authorization_ports import PolicyDecisionPoint
from app.infrastructure.security.authz.casbin_pdp import CasbinPDP, build_pdp
from app.infrastructure.security.authz.policy_document import load_policy_document


@pytest.fixture()
def pdp() -> CasbinPDP:
    return build_pdp(load_policy_document())


def req(subject: str, tool: str, category: str) -> AuthzRequest:
    return AuthzRequest.for_tool(tool=tool, category=category, subject=subject)


# --- Matrice rôles × actions ------------------------------------------------


@pytest.mark.parametrize(
    ("subject", "tool", "category", "expected"),
    [
        # viewer : lecture + system uniquement
        ("viewer", "read_file", "read", True),
        ("viewer", "list_dir", "read", True),
        ("viewer", "write_file", "write", False),
        ("viewer", "delete_path", "delete", False),
        # operator : tout ce qui est déclaré (write/delete/exec/network)
        ("operator", "write_file", "write", True),
        ("operator", "http_get", "network", True),
        ("operator", "start_training", "exec", True),
        # admin : joker tool.execute:* MAIS deny explicite pour le non déclaré
        ("admin", "write_file", "write", True),
        ("admin", "outil_bizarre", "unknown", False),
        # agent (boucle LLM) : lecture ok, capacité non déclarée refusée
        ("agent", "read_file", "read", True),
        ("agent", "outil_bizarre", "unknown", False),
        # alias MCP : read_only -> viewer, contributor -> operator
        ("read_only", "read_file", "read", True),
        ("read_only", "write_file", "write", False),
        ("contributor", "write_file", "write", True),
        # rôle fantôme : deny-by-default (aucune règle ne le couvre)
        ("ghost", "read_file", "read", False),
    ],
)
def test_authorization_matrix(
    pdp: CasbinPDP, subject: str, tool: str, category: str, *, expected: bool
) -> None:
    decision = pdp.authorize(req(subject, tool, category))
    assert decision.allowed is expected
    assert decision.policy_version == pdp.policy_version == 1


def test_deny_rule_carries_reason_and_rule_id(pdp: CasbinPDP) -> None:
    decision = pdp.authorize(req("agent", "outil_bizarre", "unknown"))
    assert not decision.allowed
    assert decision.rule.startswith("deny:")
    assert decision.reason  # raison humaine actionnable, auditable


def test_default_deny_when_no_rule_matches(pdp: CasbinPDP) -> None:
    decision = pdp.authorize(req("ghost", "read_file", "read"))
    assert not decision.allowed
    assert decision.rule == "default_deny"
    assert not decision.fail_closed  # refus POLITIQUE, pas de panne PDP


def test_implements_domain_port(pdp: CasbinPDP) -> None:
    assert isinstance(pdp, PolicyDecisionPoint)


# --- Fail-closed ------------------------------------------------------------


def test_pdp_failure_is_fail_closed(pdp: CasbinPDP, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise RuntimeError("enforcer cassé")

    monkeypatch.setattr(pdp._enforcer, "enforce", boom)
    decision = pdp.authorize(req("admin", "write_file", "write"))
    assert not decision.allowed
    assert decision.fail_closed is True
    assert decision.rule == "pdp_unavailable"


def test_decisions_are_deterministic(pdp: CasbinPDP) -> None:
    first = pdp.authorize(req("operator", "write_file", "write"))
    for _ in range(10):
        assert pdp.authorize(req("operator", "write_file", "write")) == first


# --- Performance (budget plan P2 : p95 < 5 ms local) -------------------------


def test_decision_latency_under_budget(pdp: CasbinPDP) -> None:
    request = req("agent", "read_file", "read")
    pdp.authorize(request)  # warm-up
    start = time.perf_counter()
    iterations = 300
    for _ in range(iterations):
        pdp.authorize(request)
    avg_ms = (time.perf_counter() - start) / iterations * 1000
    assert avg_ms < 5, f"décision moyenne {avg_ms:.3f} ms > budget 5 ms"
