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
from typing import Any

from app.domain.errors import BudgetExceededError

_MAX_UNBOUNDED = 10**9  # garde-fou contre un plafond non borné


@dataclass(frozen=True)
class BudgetPolicy:
    """Policy de budget partagée entre les surfaces HTTP et MCP.

    La source de vérité est la configuration effective de l'agent
    (``AgentConfig``), afin d'éviter qu'une surface applique un budget
    différente de l'autre. Le budget est ensuite matérialisé en un
    ``RunBudget`` par run.
    """

    max_llm_rounds: int = 6
    max_tool_calls: int = 20
    max_workers: int = 4
    max_events: int = 200
    max_runtime_ms: int = 30000
    max_retries: int = 2
    enforcement_mode: str = "fail_fast"

    @classmethod
    def from_config(cls, config: Any | None = None) -> BudgetPolicy:
        """Construit la policy depuis la configuration universelle du run."""
        if config is None:
            from app.agent.settings import get_agent_config

            config = get_agent_config()

        return cls(
            max_llm_rounds=int(getattr(config, "max_llm_rounds", 6)),
            max_tool_calls=int(getattr(config, "max_tool_calls", 20)),
            max_workers=int(getattr(config, "max_workers", 4)),
            max_events=int(getattr(config, "max_events", 200)),
            max_runtime_ms=int(getattr(config, "max_runtime_ms", 30000)),
            max_retries=int(getattr(config, "max_retries", 2)),
            enforcement_mode=str(getattr(config, "budget_enforcement_mode", "fail_fast")),
        )

    def to_run_budget(self) -> RunBudget:
        return RunBudget(
            max_llm_rounds=self.max_llm_rounds,
            max_tool_calls=self.max_tool_calls,
        )

    def to_dict(self) -> dict[str, int | str]:
        """Compatibility view for legacy callers and direct policy inspection.

        The total configuration is intentionally richer than the historical
        minimal budget shape, but the legacy keys stay present for backwards
        compatibility. The authoritative, shared runtime view remains
        ``to_runtime_dict()`` and ``to_trace()``.
        """
        return self.to_runtime_dict()

    def to_runtime_dict(self) -> dict[str, int | str]:
        return {
            "max_llm_rounds": self.max_llm_rounds,
            "max_tool_calls": self.max_tool_calls,
            "max_workers": self.max_workers,
            "max_events": self.max_events,
            "max_runtime_ms": self.max_runtime_ms,
            "max_retries": self.max_retries,
            "enforcement_mode": self.enforcement_mode,
        }

    def to_trace(
        self,
        *,
        llm_rounds_used: int = 0,
        tool_calls_used: int = 0,
        workers_used: int = 0,
        events_emitted: int = 0,
        runtime_ms_used: int = 0,
        retries_used: int = 0,
    ) -> dict[str, int | str]:
        """Return the canonical trace payload shared by HTTP and MCP surfaces.

        This is the single budget source of truth used by both surfaces when
        emitting audit payloads and comparing a "same scenario" run across
        transport boundaries.
        """
        trace = self.to_runtime_dict()
        trace.update(
            {
                "llm_rounds_used": llm_rounds_used,
                "tool_calls_used": tool_calls_used,
                "workers_used": workers_used,
                "events_emitted": events_emitted,
                "runtime_ms_used": runtime_ms_used,
                "retries_used": retries_used,
            }
        )
        return trace


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
