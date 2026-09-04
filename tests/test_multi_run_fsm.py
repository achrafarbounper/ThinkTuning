"""Tests de la machine à états du run superviseur multi-agents.

Couvre ``ia/agent/multi_run_fsm.py`` :
    - chemin nominal planning → dispatch → waiting_workers → synthesizing →
      completed ;
    - chemin avec validation humaine planning → … → awaiting_approval →
      resuming → synthesizing → completed ;
    - invariants : la reprise n'est possible QUE depuis ``awaiting_approval``
      (jamais depuis waiting_workers, jamais depuis un état terminal) et la
      synthèse est interdite tant qu'un worker attend une validation.

Lance : pytest tests/test_multi_run_fsm.py -v
"""

import pytest

from ia.agent.multi_run_fsm import (
    IllegalMultiRunTransition,
    MultiRunFSM,
    MultiRunState,
)


def test_nominal_path_via_fsm():
    fsm = MultiRunFSM.start()
    assert fsm.state is MultiRunState.PLANNING
    fsm = fsm.transition(MultiRunState.DISPATCH)
    fsm = fsm.transition(MultiRunState.WAITING_WORKERS)
    assert fsm.can_synthesize()  # tous les workers résolus
    fsm = fsm.transition(MultiRunState.SYNTHESIZING)
    fsm = fsm.transition(MultiRunState.COMPLETED)
    assert fsm.is_terminal()


def test_approval_path_via_fsm():
    fsm = MultiRunFSM.start()
    fsm = fsm.transition(MultiRunState.DISPATCH)
    fsm = fsm.transition(MultiRunState.WAITING_WORKERS)
    fsm = fsm.transition(MultiRunState.AWAITING_APPROVAL)
    assert fsm.is_resumable()
    # Garde : AUCUNE synthèse tant qu'un worker attend une validation.
    assert not fsm.can_synthesize()
    fsm = fsm.transition(MultiRunState.RESUMING)
    assert fsm.can_synthesize()
    fsm = fsm.transition(MultiRunState.SYNTHESIZING)
    fsm = fsm.transition(MultiRunState.COMPLETED)


def test_resume_can_request_new_approval_or_error():
    fsm = MultiRunFSM.start().transition(MultiRunState.DISPATCH)
    fsm = fsm.transition(MultiRunState.WAITING_WORKERS)
    fsm = fsm.transition(MultiRunState.AWAITING_APPROVAL)
    fsm = fsm.transition(MultiRunState.RESUMING)
    # La reprise peut déclencher une nouvelle validation…
    assert fsm.can_transition(MultiRunState.AWAITING_APPROVAL)
    # … ou échouer.
    assert fsm.can_transition(MultiRunState.ERROR)


def test_resume_only_from_awaiting_approval():
    # Invariant central : jamais de reprise hors awaiting_approval.
    fsm = MultiRunFSM.start()  # planning
    with pytest.raises(IllegalMultiRunTransition):
        fsm.transition(MultiRunState.RESUMING)

    waiting = MultiRunFSM.start().transition(MultiRunState.DISPATCH)
    waiting = waiting.transition(MultiRunState.WAITING_WORKERS)
    with pytest.raises(IllegalMultiRunTransition):
        waiting.transition(MultiRunState.RESUMING)


def test_terminal_states_are_absorbing():
    for terminal in (MultiRunState.COMPLETED, MultiRunState.ERROR):
        with pytest.raises(IllegalMultiRunTransition):
            MultiRunFSM(terminal).transition(MultiRunState.PLANNING)
        with pytest.raises(IllegalMultiRunTransition):
            MultiRunFSM(terminal).transition(MultiRunState.RESUMING)


def test_planning_failure_goes_to_error():
    fsm = MultiRunFSM.start().transition(MultiRunState.ERROR)
    assert fsm.is_terminal()
    with pytest.raises(IllegalMultiRunTransition):
        fsm.transition(MultiRunState.DISPATCH)


def test_direct_synth_from_awaiting_is_forbidden():
    """Invariant (faiblesse #5) : JAMAIS de synthèse si un worker attend."""
    fsm = MultiRunFSM.start()
    fsm = fsm.transition(MultiRunState.DISPATCH)
    fsm = fsm.transition(MultiRunState.WAITING_WORKERS)
    fsm = fsm.transition(MultiRunState.AWAITING_APPROVAL)
    assert not fsm.can_synthesize()
    with pytest.raises(IllegalMultiRunTransition):
        fsm.transition(MultiRunState.SYNTHESIZING)
    with pytest.raises(IllegalMultiRunTransition):
        fsm.transition(MultiRunState.COMPLETED)
    # Les seules issues légales : la reprise native (ou l'erreur).
    assert fsm.can_transition(MultiRunState.RESUMING)
    assert fsm.can_transition(MultiRunState.ERROR)