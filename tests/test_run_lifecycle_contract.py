# project/tests/test_run_lifecycle_contract.py
"""Contrats de coexistence Core v1 (legacy) <-> Core v2 (use-cases).

Verrouille l'alignement entre la couche application (``app/application``),
les ports typés (``app/domain/ports``) et le legacy (``core/``), par
introspection des constantes et signatures — sans ouvrir aucune base :

    - chaînes d'audit figées == constantes de ``core/audit_store.py`` ;
    - mappings de statuts == constantes du ``run_store`` legacy, couverture
      exhaustive des ``RunStatus`` et cohérence croisée API <-> store ;
    - signatures des stores legacy == signatures typées des ports (un
      Protocols ``runtime_checkable`` ne vérifie PAS les arguments :
      ce contrat comble ce trou) ;
    - bascule ``AGENT_NEW_CORE`` : priorité env, repli Settings.

Comment lancer : pytest tests/test_run_lifecycle_contract.py -v
"""

from __future__ import annotations

import inspect
import os

import pytest

from app.agent.core import RunStatus
from app.application import run_lifecycle as rl
from app.domain.ports.ports import ApprovalStorePort, RunStorePort


# --- Audit --------------------------------------------------------------------


def test_audit_action_strings_match_legacy() -> None:
    """Les chaînes d'audit figées côté application restent alignées sur le legacy."""
    from core.audit_store import ACT_APPROVAL as LEGACY_APPROVAL
    from core.audit_store import ACT_RUN as LEGACY_RUN

    assert rl.ACT_RUN == LEGACY_RUN
    assert rl.ACT_APPROVAL == LEGACY_APPROVAL


# --- Statuts ------------------------------------------------------------------


def test_decision_run_status_matches_legacy_constants() -> None:
    from core import run_store as legacy

    assert rl.DECISION_RUN_STATUS == {
        "completed": legacy.COMPLETED,
        "awaiting_approval": legacy.AWAITING_APPROVAL,
        "rejected": legacy.REJECTED,
    }


def test_core_status_maps_cover_all_run_statuses() -> None:
    from core import run_store as legacy

    assert set(rl.RUN_STATUS_TO_API) == set(RunStatus)
    assert set(rl.RUN_STATUS_TO_STORE) == set(RunStatus)
    # Tout statut store écrit par le noyau v2 est un statut valide du legacy.
    assert set(rl.RUN_STATUS_TO_STORE.values()) <= set(legacy.STATUSES)


def test_core_status_maps_api_and_store_are_consistent() -> None:
    """Cohérence croisée : statut API « error » <=> statut store ERROR, etc."""
    from core import run_store as legacy

    api_to_store = {
        "completed": legacy.COMPLETED,
        "awaiting_approval": legacy.AWAITING_APPROVAL,
        "rejected": legacy.REJECTED,
        "error": legacy.ERROR,
    }
    for status, api_status in rl.RUN_STATUS_TO_API.items():
        assert rl.RUN_STATUS_TO_STORE[status] == api_to_store[api_status]


# --- Signatures des ports vs legacy -------------------------------------------


def test_legacy_run_store_signatures_match_typed_port() -> None:
    from core.run_store import RunStore

    _assert_signature_subset(RunStorePort, RunStore)


def test_legacy_approval_store_signatures_match_typed_port() -> None:
    from core.approval_store import ApprovalStore

    _assert_signature_subset(ApprovalStorePort, ApprovalStore)


def _assert_signature_subset(port: type, legacy_cls: type) -> None:
    """Chaque méthode du port doit être appelable avec les mêmes noms
    d'arguments que l'implémentation legacy (le port ne peut exiger
    moins, jamais plus)."""
    for name in dir(port):
        if name.startswith("_"):
            continue
        port_method = getattr(port, name, None)
        legacy_method = getattr(legacy_cls, name, None)
        if not callable(port_method) or not callable(legacy_method):
            continue
        port_params = set(inspect.signature(port_method).parameters)
        legacy_params = set(inspect.signature(legacy_method).parameters)
        assert port_params <= legacy_params, (
            f"{legacy_cls.__name__}.{name} : le port exige {port_params - legacy_params}"
        )


# --- Bascule AGENT_NEW_CORE -----------------------------------------------------


def test_new_core_env_takes_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.agent.factory import new_core_enabled

    monkeypatch.setenv("AGENT_NEW_CORE", "1")
    assert new_core_enabled() is True
    monkeypatch.setenv("AGENT_NEW_CORE", "0")
    assert new_core_enabled() is False
    monkeypatch.setenv("AGENT_NEW_CORE", "yes")
    assert new_core_enabled() is True


def test_new_core_settings_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sans variable d'environnement, le repli Settings s'applique
    (y compris la valeur lue depuis le fichier .env par pydantic-settings)."""
    from app.agent.factory import new_core_enabled
    from app.config.settings import get_settings

    monkeypatch.delenv("AGENT_NEW_CORE", raising=False)
    get_settings.cache_clear()
    # Défaut : noyau v2 ACTIVÉ (bascule en production) — y compris si les
    # Settings ne sont pas chargeables (repli fail-open de new_core_enabled).
    assert new_core_enabled() is True
    os.environ["AGENT_NEW_CORE"] = "0"
    get_settings.cache_clear()
    try:
        assert new_core_enabled() is False
    finally:
        os.environ.pop("AGENT_NEW_CORE", None)
        get_settings.cache_clear()


def test_settings_expose_new_core_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Le snapshot des flags inclut la bascule du noyau (observabilité)."""
    from app.config.settings import get_settings

    monkeypatch.setenv("AGENT_NEW_CORE", "1")
    get_settings.cache_clear()
    try:
        settings = get_settings()
    except Exception:
        pytest.skip("Settings non chargeables dans cet environnement (.env incomplet)")
    assert settings.flag_new_core is True
    assert settings.active_flags()["new_core"] is True
    get_settings.cache_clear()
