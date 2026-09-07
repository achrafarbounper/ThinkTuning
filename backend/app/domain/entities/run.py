"""Cycle de vie d'un run agent — modèle de domaine, sans aucune I/O.

Source de vérité du statut terminal du noyau v2 (``RunStatus``) et validation
pure des transitions de la durée de vie PERSISTÉE d'un run (``RunStateMachine``).

Pourquoi dans le domaine (règle d'or) :
    - ``RunStatus`` était défini dans le moteur ``app/agent/core.py`` et
      importé par la couche application — un couplage application → moteur ;
    - les transitions étaient éparses (``if`` dans les routes / use-cases).
    Ici, la reprise ``awaiting_approval → running`` (empreinte validée) est
    une transition VALIDÉE par le domaine : aucune logique d'état ne devrait
    survivre ailleurs.

Les constantes d'état persisté ci-dessous sont alignées sur le run_store
legacy (``core/run_store.py``) ; un test de contrat (convention
``test_*_match_legacy``) verrouille cet alignement sans import legacy ici.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import ClassVar


class RunStatus(StrEnum):
    """Statut terminal d'un run du noyau agentique.

    Déplacé de ``app/agent/core.py`` vers le domaine : c'est un concept
    métier de cycle de vie, pas un détail de moteur.
    """

    COMPLETED = "completed"                # réponse finale produite
    PENDING_APPROVAL = "pending_approval"  # action en attente de validation
    REJECTED_LOOP = "rejected_loop"        # le LLM reformule une action rejetée
    BUDGET_EXHAUSTED = "budget_exhausted"  # budget épuisé sans réponse finale
    FAILED = "failed"                      # erreur non récupérable


# --- Statuts PERSISTÉS d'un run (alignés sur core/run_store.py) ------------------

RUNNING = "running"
COMPLETED = "completed"
AWAITING_APPROVAL = "awaiting_approval"
REJECTED = "rejected"
ERROR = "error"

_PERSISTED_STATUSES = frozenset(
    {RUNNING, COMPLETED, AWAITING_APPROVAL, REJECTED, ERROR}
)

# États terminaux : aucune transition sortante.
_TERMINAL = frozenset({COMPLETED, REJECTED, ERROR})


class IllegalRunTransition(ValueError):
    """Transition de statut de run non autorisée par la machine à états.

    Levée par ``RunStateMachine.transition`` quand une issue est demandée pour
    un run dont l'état courant ne la permet pas (ex. clôturer un run déjà
    terminal, ou reprise d'un run ``completed``).
    """

    def __init__(self, current: str, requested: str) -> None:
        self.current = current
        self.requested = requested
        super().__init__(
            f"Transition de run illégale : '{current}' -> '{requested}'"
        )


@dataclass(frozen=True)
class RunStateMachine:
    """Machine à états PURE de la durée de vie persistée d'un run.

    Les seules transitions valides :
        ``running``        -> completed | awaiting_approval | rejected | error
        ``awaiting_approval`` -> running (REPRISE par empreinte)
                             | completed | rejected | error
        terminales (completed/rejected/error) : absorbantes (aucune sortie).

    La reprise ``awaiting_approval -> running`` est l'invariant central que
    ce module rend explicite : un run ne peut être relancé QUE s'il était en
    attente de validation humaine (jamais depuis un état terminal).

    Usage : les use-cases construisent ``RunStateMachine(status)`` puis
    ``transition(destination)`` avant d'appeler ``finish_run`` ; toute issue
    illégale est un bug de programmation, échouant tôt et proprement.
    """

    status: str = RUNNING

    _TRANSITIONS: ClassVar[dict[str, frozenset[str]]] = {
        RUNNING: frozenset({COMPLETED, AWAITING_APPROVAL, REJECTED, ERROR}),
        AWAITING_APPROVAL: frozenset(
            {RUNNING, COMPLETED, REJECTED, ERROR}
        ),
        COMPLETED: frozenset(),
        REJECTED: frozenset(),
        ERROR: frozenset(),
    }

    def __post_init__(self) -> None:
        if self.status not in _PERSISTED_STATUSES:
            raise ValueError(f"Statut de run inconnu : '{self.status}'")

    # --- API --------------------------------------------------------------

    def can_transition(self, destination: str) -> bool:
        """True si ``destination`` est une issue légale depuis l'état courant."""
        return destination in self._TRANSITIONS.get(self.status, frozenset())

    def transition(self, destination: str) -> RunStateMachine:
        """Retourne la machine à l'état ``destination`` ou lève ValueError."""
        if not self.can_transition(destination):
            raise IllegalRunTransition(self.status, destination)
        return replace(self, status=destination)

    def is_terminal(self) -> bool:
        """Vrai si l'état courant est terminal (absorbant)."""
        return self.status in _TERMINAL

    def is_resumable(self) -> bool:
        """Vrai si le run peut être repris (attente de validation humaine)."""
        return self.status == AWAITING_APPROVAL

    # --- Fabriques --------------------------------------------------------

    @classmethod
    def start(cls) -> RunStateMachine:
        """Un run fraîchement ouvert est ``running`` (source de vérité unique)."""
        return cls(RUNNING)
