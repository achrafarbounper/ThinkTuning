"""Machine à états de l'ORCHESTRATION multi-agents (pure, sans aucune I/O).

Vie à côté du moteur ``ia/agent/orchestrator.py`` (ia n'importe PAS app/ —
règle d'or des dépendances du projet) : mêmes conventions que
``app/domain/entities/run.py`` (machine à états du run mono-agent), pure et
testable sans I/O.

Cycle de vie d'un run SUPERVISEUR :

    planning -> dispatch -> waiting_workers -> awaiting_approval -> resuming
        |            |            |             |
        |            |            |             +------------> synthesizing -> completed
        |            |            +--------------------------> synthesizing
        |            +---------------------------------------> synthesizing
        |---> fallback_chat (intention « chat » : plan vide ou tout filtré)
        |            |
        +------------+---------------------------------------> synthesizing -> completed
        +-----------------------------------------------------> error

Invariants rendus explicites par ce module :
    - la reprise ``awaiting_approval -> resuming`` est la SEULE transition qui
      relance un run interrompu : un run ne peut PAS être repris depuis un état
      terminal ni depuis ``waiting_workers`` (jamais de contournement de
      l'orchestrateur) ;
    - la synthèse n'est EXÉCUTABLE que depuis ``awaiting_approval`` (via
      ``resuming``), ``waiting_workers`` ou ``fallback_chat`` : jamais tant
      qu'un worker est en attente de validation (garde portée par
      ``can_synthesize``) ;
    - ``fallback_chat`` (Approche B) est un repli MANDATOIRE : quand
      l'intention détectée rend TOUT le plan hors périmètre (workers filtrés)
      ou que le planner renvoie un plan vide sur une intention « chat », le
      superviseur répond directement — jamais de silence utilisateur.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum


class MultiRunState(StrEnum):
    """États de la machine à états d'un run superviseur."""

    PLANNING = "planning"
    DISPATCH = "dispatch"
    WAITING_WORKERS = "waiting_workers"
    AWAITING_APPROVAL = "awaiting_approval"
    RESUMING = "resuming"
    FALLBACK_CHAT = "fallback_chat"
    SYNTHESIZING = "synthesizing"
    COMPLETED = "completed"
    ERROR = "error"


_TERMINAL = frozenset({MultiRunState.COMPLETED, MultiRunState.ERROR})

_TRANSITIONS: dict[MultiRunState, frozenset[MultiRunState]] = {
    MultiRunState.PLANNING: frozenset({
        MultiRunState.DISPATCH,
        MultiRunState.FALLBACK_CHAT,  # plan vide + intention « chat » → réponse directe
        MultiRunState.ERROR,
    }),
    MultiRunState.DISPATCH: frozenset(
        {MultiRunState.WAITING_WORKERS, MultiRunState.ERROR}
    ),
    MultiRunState.WAITING_WORKERS: frozenset({
        MultiRunState.AWAITING_APPROVAL,
        MultiRunState.FALLBACK_CHAT,  # toutes les sous-tâches filtrées par l'intention
        MultiRunState.SYNTHESIZING,
        MultiRunState.ERROR,
    }),
    MultiRunState.AWAITING_APPROVAL: frozenset({
        MultiRunState.RESUMING,   # SEULE issue de poursuite (reprise native)
        MultiRunState.ERROR,      # snapshot expiré / demande non approuvée
    }),
    MultiRunState.RESUMING: frozenset({
        MultiRunState.WAITING_WORKERS,    # re-dispatch du worker repris
        MultiRunState.SYNTHESIZING,       # reprise aboutie → synthèse finale
        MultiRunState.AWAITING_APPROVAL,  # nouvelle validation requise
        MultiRunState.ERROR,
    }),
    MultiRunState.FALLBACK_CHAT: frozenset({
        MultiRunState.SYNTHESIZING,  # la réponse conversationnelle devient la réponse finale
        MultiRunState.ERROR,
    }),
    MultiRunState.SYNTHESIZING: frozenset({
        MultiRunState.COMPLETED,
        MultiRunState.ERROR,
    }),
    MultiRunState.COMPLETED: frozenset(),
    MultiRunState.ERROR: frozenset(),
}


class IllegalMultiRunTransition(ValueError):
    """Transition d'orchestration illégale (bug de programmation, échec tôt)."""

    def __init__(self, current: MultiRunState, requested: MultiRunState):
        self.current = current
        self.requested = requested
        super().__init__(f"Transition multi illégale : '{current}' -> '{requested}'")


@dataclass(frozen=True)
class MultiRunFSM:
    """Machine à états pure du cycle superviseur (voir docstring de module)."""

    state: MultiRunState = MultiRunState.PLANNING

    # --- API --------------------------------------------------------------

    def can_transition(self, destination: MultiRunState) -> bool:
        return destination in _TRANSITIONS.get(self.state, frozenset())

    def transition(self, destination: MultiRunState) -> MultiRunFSM:
        if not self.can_transition(destination):
            raise IllegalMultiRunTransition(self.state, destination)
        return replace(self, state=destination)

    def is_terminal(self) -> bool:
        return self.state in _TERMINAL

    def is_resumable(self) -> bool:
        """Vrai si le run peut être repris (attente de validation humaine)."""
        return self.state is MultiRunState.AWAITING_APPROVAL

    def can_synthesize(self) -> bool:
        """Vrai si la phase de synthèse est légale depuis l'état courant.

        Règle d'or : produire une synthèse alors qu'une sous-tâche attend une
        validation serait trompeuse → uniquement depuis ``waiting_workers``
        (tous les workers résolus) ou ``resuming`` (reprise aboutie).
        """
        return self.can_transition(MultiRunState.SYNTHESIZING)

    # --- Fabriques ----------------------------------------------------------

    @classmethod
    def start(cls) -> MultiRunFSM:
        """Un run fraîchement ouvert commence en ``planning``."""
        return cls(MultiRunState.PLANNING)
