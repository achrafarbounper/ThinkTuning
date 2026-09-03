"""Tests du port ``ContextPort`` et de ses adaptateurs (strangler + déterministe).

Objectif : verrouiller la coexistence Core v1 / v2 pour LA gestion du contexte
sans ouvrir de base ni appeler le réseau.

- conformité structurelle : adaptateurs satisfont ``ContextPort`` (signatures) ;
- équivalence comportementale : le wrapper legacy se comporte exactement comme
  le module legacy ``ia.agent.context`` (délégation réelle) ;
- profil ``AGENT_CONTEXT=0`` : ``NullContextProvider`` ne mute ni ne résume ;
- intégration : le fournisseur par défaut bascule selon la valeur du flag.
"""

from __future__ import annotations

import inspect

import pytest

from app.domain.ports import ContextPort
from app.infrastructure.context import (
    LegacyContextProvider,
    NullContextProvider,
    default_context_provider,
)

PORT_METHODS = ("estimate_tokens", "optimize_history",
                "update_memory_summary", "format_memory_note")

ADAPTERS = [LegacyContextProvider, NullContextProvider]


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda c: c.__name__)
def test_adapter_is_context_port(adapter_cls):
    assert issubclass(adapter_cls, ContextPort)


@pytest.mark.parametrize("adapter_cls", ADAPTERS, ids=lambda c: c.__name__)
def test_adapter_implements_full_port_signature(adapter_cls):
    """Le port n'exige que des arguments que CHAQUE adaptateur accepte."""
    for name in PORT_METHODS:
        port_params = set(
            inspect.signature(getattr(ContextPort, name)).parameters
        )
        impl_params = set(
            inspect.signature(getattr(adapter_cls, name)).parameters
        )
        assert port_params <= impl_params, (
            f"{adapter_cls.__name__}.{name} : {port_params - impl_params}"
        )


# --- Équivalence comportementale du wrapper legacy ---------------------------


def test_legacy_wrapper_delegates_to_legacy_module():
    """Le wrapper legacy === les fonctions pures de ia/agent/context (v1)."""
    from ia.agent import context as _legacy

    wrapper = LegacyContextProvider()
    history = [{"role": "user", "content": "salut"}] * 100

    assert wrapper.estimate_tokens("12345678") == _legacy.estimate_tokens("12345678")

    opt_wrapper = wrapper.optimize_history(history, max_tokens=100)
    opt_legacy = _legacy.optimize_history(history, max_tokens=100)
    assert opt_wrapper == opt_legacy
    mb_meta = opt_wrapper[1]
    assert mb_meta["kept"] > 0 and mb_meta["dropped"] > 0

    assert wrapper.update_memory_summary(
        "début", "p", "r", max_chars=50
    ) == _legacy.update_memory_summary("début", "p", "r", max_chars=50)

    assert wrapper.format_memory_note("") is None
    assert wrapper.format_memory_note("") == _legacy.format_memory_note("")
    assert wrapper.format_memory_note("résumé") == _legacy.format_memory_note("résumé")


def test_legacy_wrapper_optimize_history_preserves_order_and_meta():
    """Fenêtre glissante : les tours les plus récents survivent au résumé."""
    wrapper = LegacyContextProvider()
    history = [{"role": "user", "content": f"msg-{i}"} for i in range(10)]
    kept, meta = wrapper.optimize_history(history, max_tokens=60)
    # La méta est complète et l'ordre chronologique est reconstruit.
    assert set(meta) >= {"kept", "dropped", "summarized", "estimated_tokens"}
    contents = [m["content"] for m in kept]
    assert contents[-1] == "msg-9"


# --- Profil AGENT_CONTEXT=0 : adaptateur déterministe ------------------------


def test_null_provider_does_not_mutate_history():
    n = NullContextProvider()
    history = [{"role": "user", "content": "bonjour"}]
    kept, meta = n.optimize_history(history, max_tokens=1)
    assert kept == history                     # identiques, aucune troncation
    assert meta["dropped"] == 0 and meta["summarized"] is False
    assert n.format_memory_note("") is None
    assert n.format_memory_note("r") is not None


def test_null_provider_estimate_is_positive():
    n = NullContextProvider()
    assert n.estimate_tokens("") == 0
    assert n.estimate_tokens("abcd") == 1
    assert n.estimate_tokens("abcdabcd") == 2


# --- Bascule du fournisseur par défaut --------------------------------------


def test_default_context_provider_respects_flag():
    assert isinstance(default_context_provider(context_enabled=True),
                      LegacyContextProvider)
    assert isinstance(default_context_provider(context_enabled=False),
                      NullContextProvider)
