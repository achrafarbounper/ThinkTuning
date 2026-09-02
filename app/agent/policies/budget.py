"""Policy de budget d'un run agent : plafonds matériels du plan → action.

Rôle dans la boucle agentique : empêcher tout dérégagement (LLM qui boucle,
outil appelé en rafale, run infini). Chaque consommation est validée par un
``consume_*`` qui lève ``BudgetExceededError`` (fail-fast, mappé 429 par
l'API) dès qu'un plafond est franchi.

Plafonds par défaut issus de ``app/config/settings.py`` :
    - ``agent_max_llm_rounds``  : rounds LLM max par run (défaut 6, aligné
      sur MAX_LLM_ROUNDS de ia/agent/agent_core.py) ;
    - ``agent_max_tool_calls``  : appels d'outils max par run (défaut 20).

Conception :
    - mutable volontairement (compteur d'état d'un run), mais mono-thread par
      construction : un run appartient à un seul flux d'exécution ;
    - ``snapshot()`` fournit l'état pour l'audit et les événements SSE.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.errors import BudgetExceededError

_MAX_UNBOUNDED = 10**9  # garde-fou contre un plafond non borné


@dataclass
class BudgetSnapshot:
    """État consommable du budget (audit / événements / tests)."""

    llm_rounds_used: int
    llm_rounds_max: int
    tool_calls_used: int
    tool_calls_max: int

    def to_dict(self) -> dict[str, int]:
        return {
            "llm_rounds_used": self.llm_rounds_used,
            "llm_rounds_max": self.llm_rounds_max,
            "tool_calls_used": self.tool_calls_used,
            "tool_calls_max": self.tool_calls_max,
        }


@dataclass
class RunBudget:
    """Budget consommable d'un run agent (rounds LLM + appels d'outils)."""

    max_llm_rounds: int = 6
    max_tool_calls: int = 20
    _llm_rounds: int = field(default=0, init=False, repr=False)
    _tool_calls: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.max_llm_rounds < 1 or self.max_tool_calls < 1:
            raise ValueError("Les plafonds de budget doivent être >= 1")
        # Plafonds non bornés interdits : un budget infini n'est pas un budget.
        self.max_llm_rounds = min(self.max_llm_rounds, _MAX_UNBOUNDED)
        self.max_tool_calls = min(self.max_tool_calls, _MAX_UNBOUNDED)

    # --- Consommation (fail-fast) -------------------------------------------

    def consume_llm_round(self) -> int:
        """Consomme un round LLM ; renvoie le numéro du round (1-based)."""
        if self._llm_rounds >= self.max_llm_rounds:
            raise BudgetExceededError(
                f"Budget LLM épuisé : {self._llm_rounds}/{self.max_llm_rounds} rounds",
                details=self.snapshot().to_dict(),
            )
        self._llm_rounds += 1
        return self._llm_rounds

    def consume_tool_call(self, tool: str = "") -> int:
        """Consomme un appel d'outil ; renvoie le numéro d'appel (1-based)."""
        if self._tool_calls >= self.max_tool_calls:
            label = f" (outil {tool!r})" if tool else ""
            raise BudgetExceededError(
                f"Budget d'outils épuisé : {self._tool_calls}/{self.max_tool_calls} appels{label}",
                details=self.snapshot().to_dict(),
            )
        self._tool_calls += 1
        return self._tool_calls

    # --- Lecture -------------------------------------------------------------

    @property
    def llm_rounds_left(self) -> int:
        return self.max_llm_rounds - self._llm_rounds

    @property
    def tool_calls_left(self) -> int:
        return self.max_tool_calls - self._tool_calls

    @property
    def exhausted(self) -> bool:
        return self.llm_rounds_left <= 0 or self.tool_calls_left <= 0

    def snapshot(self) -> BudgetSnapshot:
        return BudgetSnapshot(
            llm_rounds_used=self._llm_rounds,
            llm_rounds_max=self.max_llm_rounds,
            tool_calls_used=self._tool_calls,
            tool_calls_max=self.max_tool_calls,
        )
