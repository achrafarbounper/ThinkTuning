# project/tests/test_authz_policy_document.py
"""Tests du document de politique VERSIONNÉ (P2 Lot A) — validation fail-closed."""

from __future__ import annotations

import pytest

from app.infrastructure.security.authz.policy_document import (
    PolicyDocument,
    load_policy_document,
)

VALID = {
    "policy_version": 2,
    "default_tenant_mode": "strict",
    "tenant_modes": {"default": "strict", "sandbox": "legacy_permissive"},
    "role_aliases": {"read_only": "viewer"},
    "roles": {"viewer": ["tool.execute:read", "tool.execute:*"]},
    "denies": [{"role": "*", "action": "tool.execute:unknown", "reason": "non déclaré"}],
}


def test_default_policy_loads_and_is_versioned() -> None:
    policy = load_policy_document()
    assert isinstance(policy, PolicyDocument)
    assert policy.policy_version >= 1
    assert policy.default_tenant_mode.value in ("legacy_permissive", "strict")
    # Le document par défaut DOIT couvrir les rôles canoniques + alias MCP.
    for role in ("viewer", "operator", "admin", "agent", "service"):
        assert role in policy.roles
    assert policy.role_aliases.get("read_only") == "viewer"
    assert policy.role_aliases.get("contributor") == "operator"
    # La règle dure : toute capacité NON déclarée est refusée.
    assert any(rule.action == "tool.execute:unknown" for rule in policy.denies)


def test_load_from_dict_validates_strictly() -> None:
    policy = load_policy_document(VALID)
    assert policy.policy_version == 2
    assert policy.tenant_mode("sandbox").value == "legacy_permissive"
    assert policy.tenant_mode("inconnu").value == "strict"  # défaut
    assert policy.default_tenant_mode.value == "strict"


@pytest.mark.parametrize(
    "broken",
    [
        {**VALID, "policy_version": 0},  # version non positive
        {k: v for k, v in VALID.items() if k != "policy_version"},  # version absente
        {**VALID, "champ_inconnu": True},  # champ hors schéma (extra=forbid)
        {**VALID, "default_tenant_mode": "yolo"},  # mode inconnu
        {**VALID, "roles": {"viewer": []}},  # rôle sans pattern (fail-closed)
        {**VALID, "roles": {"": ["tool.execute:read"]}},  # nom de rôle vide
    ],
)
def test_invalid_documents_are_rejected(broken: dict) -> None:
    with pytest.raises(ValueError):
        load_policy_document(broken)


def test_deny_rule_matching_is_wildcard_aware() -> None:
    policy = load_policy_document(VALID)
    hit = policy.is_denied(role="agent", action="tool.execute:unknown", resource="x")
    assert hit is not None and "non déclaré" in hit.reason
    miss = policy.is_denied(role="agent", action="tool.execute:read", resource="x")
    assert miss is None


def test_canonical_role_resolves_aliases_only() -> None:
    policy = load_policy_document(VALID)
    assert policy.canonical_role("read_only") == "viewer"
    # Un sujet inconnu n'est PAS inventé : deny-by-default s'appliquera.
    assert policy.canonical_role("ghost") == "ghost"


def test_fingerprint_is_stable_and_changes_with_content() -> None:
    a = load_policy_document(VALID)
    b = load_policy_document(VALID)
    c = load_policy_document({**VALID, "policy_version": 3})
    assert a.fingerprint() == b.fingerprint()
    assert a.fingerprint() != c.fingerprint()
