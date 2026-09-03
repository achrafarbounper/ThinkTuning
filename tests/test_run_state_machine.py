"""Contrat de la machine à états du run — domaine pur, zéro I/O.

Vérifie :
    - le déplacement de ``RunStatus`` : source de vérité dans le domaine, et
      ``app.agent.core`` ré-exporte le même objet (rétro-compatibilité) ;
    - les transitions autorisées/interdites de ``RunStateMachine``, dont
      l'invariant central : la reprise ``awaiting_approval -> running`` est la
      SEULE façon de relancer un run (jamais depuis un état terminal) ;
    - le verrouillage dans ``run_lifecycle.finish_run_status`` (les use-cases
      passent par la FSM avant ``finish_run``).
"""

from __future__ import annotations

import pytest

from app.agent.core import RunStatus as CoreRunStatus
from app.application import run_lifecycle as rl
from app.domain.entities.run import (
    AWAITING_APPROVAL,
    COMPLETED,
    ERROR,
    REJECTED,
    RUNNING,
    IllegalRunTransition,
    RunStateMachine,
    RunStatus,
)


def test_run_status_single_source_of_truth_in_domain():
    """```core.py`` ré-exporte l'enum du domaine (pas une copie)."""
    assert CoreRunStatus is RunStatus
    assert RunStatus.COMPLETED.value == "completed"
    assert RunStatus.PENDING_APPROVAL.value == "pending_approval"


def test_start_is_running():
    assert RunStateMachine.start().status == RUNNING
    assert not RunStateMachine.start().is_terminal()
    assert not RunStateMachine.start().is_resumable()


def test_running_can_finish_terminally_or_pause():
    start = RunStateMachine.start()
    for dest in (COMPLETED, AWAITING_APPROVAL, REJECTED, ERROR):
        assert start.can_transition(dest)
    # running n'est PAS résumable et n'est pas terminal.
    assert not start.is_resumable()


def test_transition_returns_new_machine():
    ended = RunStateMachine.start().transition(COMPLETED)
    assert ended.status == COMPLETED
    assert ended.is_terminal()
    # Le statut d'origine est préservé : la machine est immuable.
    assert RunStateMachine.start().status == RUNNING


def test_illegal_transition_raises():
    # Un run terminal est absorbant : aucune sortie.
    ended = RunStateMachine.start().transition(COMPLETED)
    assert not ended.can_transition(RUNNING)
    with pytest.raises(IllegalRunTransition):
        ended.transition(RUNNING)
    # Statut inconnu → ValueError.
    with pytest.raises(ValueError):
        RunStateMachine("bogus")


def test_awaiting_approval_is_the_only_resumable_state():
    paused = RunStateMachine.start().transition(AWAITING_APPROVAL)
    assert paused.is_resumable()
    assert paused.can_transition(RUNNING)          # reprise par empreinte
    assert paused.can_transition(COMPLETED)        # ou clôture directe
    # Depuis un état terminal, plus jamais de relance.
    assert not RunStateMachine.start().transition(REJECTED).can_transition(RUNNING)


def test_full_approval_lifecycle_via_fsm():
    """running -> awaiting_approval -> running -> completed (reprise puis fin)."""
    fsm = RunStateMachine.start()
    fsm = fsm.transition(AWAITING_APPROVAL)
    fsm = fsm.transition(RUNNING)                  # empreinte validée
    fsm = fsm.transition(COMPLETED)
    assert fsm.status == COMPLETED
    assert fsm.is_terminal()


def test_finish_run_status_validation_in_lifecycle():
    # Les use-cases ferment depuis `running` : les 4 issues sont légales.
    assert rl.finish_run_status(RUNNING, COMPLETED) == COMPLETED
    assert rl.finish_run_status(RUNNING, AWAITING_APPROVAL) == AWAITING_APPROVAL
    assert rl.finish_run_status(RUNNING, REJECTED) == REJECTED
    assert rl.finish_run_status(RUNNING, ERROR) == ERROR
    # Défaut = `running` pour un run fraîchement ouvert.
    assert rl.finish_run_status(None, COMPLETED) == COMPLETED
    # Un run déjà terminal ne peut être re-clôturé (bug de programmation).
    with pytest.raises(IllegalRunTransition):
        rl.finish_run_status(COMPLETED, RUNNING)


def test_mappings_consistent_with_domain_statuses():
    # Couverture exhaustive des RunStatus dans les mappings API / store.
    assert set(rl.RUN_STATUS_TO_API) == set(RunStatus)
    assert set(rl.RUN_STATUS_TO_STORE) == set(RunStatus)
    # Realisable : tout statut v2→store est un statut run légal de la FSM.
    for v2 in RunStatus:
        stored = rl.RUN_STATUS_TO_STORE[v2]
        assert RunStateMachine(RUNNING).can_transition(stored)