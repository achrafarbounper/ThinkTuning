"""Boucle agentique : Intent → Plan → Policy → Budget → Action → Réponse.

Moteur applicatif du nouveau noyau, bâti EXCLUSIVEMENT sur les ports du
domaine (``LLMClientPort``, ``ToolRegistryPort``) — testable sans réseau ni
SQLite via des fakes. Complète (et à terme remplace) la boucle historique de
``ia/agent/agent_core.py`` en respectant ses leçons :

    1. le system prompt est GÉNÉRÉ depuis le registre réel des outils (le
       LLM connaît noms/arguments et sait quand les appeler) ;
    2. une réponse en texte normal SANS JSON est légitime (salutation,
       explication) et renvoyée telle quelle ;
    3. auto-correction : outil inconnu, arguments manquants ou erreur
       d'exécution sont renvoyés AU LLM avec la liste des outils valides
       (jusqu'à épuisement du budget) ;
    4. fail-fast et garde-fous : budget (rounds/appels), policy de sandbox
       (REJECT bloquant et audité, APPROVE → validation humaine).

Décisions de policy intégrées au flux :
    - ``AUTO_APPROVE`` → exécution immédiate ;
    - ``APPROVE``      → l'action est mise en attente : le run se termine avec
      ``status=pending_approval`` et l'action en ``awaiting`` (le flux
      d'approbation humaine passe par ``core/approval_store`` côté legacy, ou
      un ``ApprovalStorePort`` injecté côté nouveau noyau) ;
    - ``REJECT``       → l'action est refusée, le LLM est informé (il peut
      reformuler) ; répétition d'un rejet identique → arrêt immédiat
      (anti-boucle, empreinte de l'action).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from app.agent.policies.budget import RunBudget
from app.agent.policies.sandbox_policy import decide_action
from app.domain.entities.plan import Action, Decision, Intent, Plan, PlanStep
from app.domain.errors import BudgetExceededError
from app.domain.ports import LLMClientPort, Message, ToolRegistryPort

logger = logging.getLogger("thinktuning.agent.core")

# ============================================================
# MODÈLES DE RÉSULTAT
# ============================================================


class RunStatus(StrEnum):
    """Statut terminal d'un run du noyau agentique."""

    COMPLETED = "completed"                # réponse finale produite
    PENDING_APPROVAL = "pending_approval"  # action en attente de validation
    REJECTED_LOOP = "rejected_loop"        # le LLM reformule une action rejetée
    BUDGET_EXHAUSTED = "budget_exhausted"  # budget épuisé sans réponse finale
    FAILED = "failed"                      # erreur non récupérable


class ActionTrace(BaseModel):
    """Trace sérialisable d'une action exécutée (audit, SSE, tests)."""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    decision: str
    status: str                      # done / error / awaiting_approval / rejected
    result_summary: str = ""
    error: str = ""

    model_config = {"frozen": True}


class AgentRunResult(BaseModel):
    """Résultat complet d'un run (réponse, traces, budget consommé)."""

    answer: str = ""
    status: RunStatus = RunStatus.COMPLETED
    thinking: str = ""
    actions: list[ActionTrace] = Field(default_factory=list)
    awaiting_action: Action | None = None   # action pending_approval le cas échéant
    rounds_used: int = 0
    tool_calls_used: int = 0

    model_config = {"frozen": True}


# ============================================================
# SYSTEM PROMPT (généré depuis le registre réel des outils)
# ============================================================

_SYSTEM_PROMPT_HEADER = """Tu es un agent outillé. Tu peux utiliser les outils suivants :

{tools}

PROTOCOLE (strict) :
1. Si tu as besoin d'un outil, réponds UNIQUEMENT avec un JSON :
   {{"plan": [{{"tool": "<nom>", "args": {{...}}}}, ...]}}
2. Si tu n'as besoin d'aucun outil, réponds en texte normal (pas de JSON).
3. Après avoir reçu les résultats, conclus en t'appuyant UNIQUEMENT sur eux.
4. N'invente jamais d'outil ni d'argument : {tool_names} sont les seuls outils.
"""


def build_system_prompt(registry: ToolRegistryPort) -> str:
    """Génère le prompt système à partir des métadonnées réelles du registre.

    Sans cela, le modèle devine ses propres outils et répond de mémoire
    (bug historique documenté dans ia/agent/agent_core.py)."""
    descriptions: list[str] = []
    for name in registry.tool_names():
        meta = registry.meta(name) or {}
        desc = meta.get("description") or meta.get("description", "")
        required = meta.get("required_args") or []
        line = f"- {name}: {desc}"
        if required:
            line += f" (args requis : {', '.join(map(str, required))})"
        descriptions.append(line)
    names = ", ".join(registry.tool_names())
    return _SYSTEM_PROMPT_HEADER.format(tools="\n".join(descriptions), tool_names=names)


# ============================================================
# PARSING TOLÉRANT DU PLAN
# ============================================================

_PLAN_KEYS = ("plan", "tasks", "actions")


def extract_plan(raw: str) -> Plan | None:
    """Extrait un ``Plan`` d'une réponse LLM (tolérant prose/fences markdown).

    Formes acceptées : liste directe, {plan|tasks|actions: [...]}, JSON
    entouré de texte. Renvoie None si rien d'exploitable (=> réponse texte).
    """
    import json
    import re

    if not raw or not raw.strip():
        return None
    candidates: list[str] = [raw.strip()]
    # Fences markdown ```json ... ``` et blocs {…} isolés dans la prose.
    candidates.extend(m.strip() for m in re.findall(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL))
    candidates.extend(m.strip() for m in re.findall(r"\{.*\}", raw, re.DOTALL))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, list):
            return _steps_from(parsed)
        if isinstance(parsed, dict):
            for key in _PLAN_KEYS:
                if isinstance(parsed.get(key), list):
                    return _steps_from(parsed[key])
    return None


def _steps_from(items: list[Any]) -> Plan | None:
    steps = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            return None
        tool = item.get("tool")
        if not tool:
            return None
        steps.append(
            PlanStep(task_id=str(item.get("task_id") or f"step-{index + 1}"),
                     action=Action(tool=str(tool), args=dict(item.get("args") or {})))
        )
    try:
        return Plan(steps=steps)
    except Exception:
        return None


# ============================================================
# NOYAU AGENTIQUE
# ============================================================

_RESULT_SUMMARY_CHARS = 400


class AgentCore:
    """Boucle Intent → Plan → Policy → Budget → Action → Réponse.

    Args:
        llm:       client LLM (port) ;
        registry:  registre d'outils (port) ;
        max_rounds:      plafond de rounds LLM (défaut Settings.agent_max_llm_rounds) ;
        max_tool_calls:  plafond d'appels d'outils (défaut Settings.agent_max_tool_calls) ;
        approval_gateway: callback optionnel ``(Action) -> bool``. Absent =>
                           toute action APPROVE met le run en pending_approval.
    """

    def __init__(
        self,
        llm: LLMClientPort,
        registry: ToolRegistryPort,
        *,
        max_rounds: int = 6,
        max_tool_calls: int = 20,
        approval_gateway: Callable[[Action], bool] | None = None,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._approval_gateway = approval_gateway
        self._max_tool_calls = max_tool_calls
        self._system_prompt = build_system_prompt(registry)

    def run(self, intent: Intent, history: list[Message] | None = None) -> AgentRunResult:
        """Exécute un run complet. Ne lève JAMAIS : le statut porte l'échec."""
        budget = RunBudget(max_llm_rounds=intent.max_rounds, max_tool_calls=self._max_tool_calls)
        traces: list[ActionTrace] = []
        rejected_prints: set[str] = set()
        messages: list[Message] = [
            {"role": "system", "content": self._system_prompt},
            *(history or []),
            {"role": "user", "content": intent.prompt},
        ]

        while True:
            try:
                budget.consume_llm_round()
            except BudgetExceededError:
                return self._finalize(traces, budget, RunStatus.BUDGET_EXHAUSTED)

            try:
                response = self._llm.call(messages)
            except Exception as exc:
                logger.error("Échec du client LLM : %s", exc)
                return self._finalize(traces, budget, RunStatus.FAILED,
                                      answer=f"Erreur LLM : {exc}")
            plan = extract_plan(response)

            if plan is None:  # réponse texte directe (légitime)
                return self._finalize(
                    traces, budget, RunStatus.COMPLETED, answer=response.strip()
                )

            outcome = self._execute_plan(plan, budget, traces, rejected_prints)
            if isinstance(outcome, AgentRunResult):
                return outcome
            # Continuation : résultats d'outils renvoyés au LLM (auto-correction)
            messages = [*messages, {"role": "assistant", "content": response},
                        {"role": "user", "content": outcome}]

        # --- Exécution d'un plan ----------------------------------------------------

    def _execute_plan(
        self,
        plan: Plan,
        budget: RunBudget,
        traces: list[ActionTrace],
        rejected_prints: set[str],
    ) -> AgentRunResult | str:
        """Exécute les actions du plan.

        Renvoie un ``AgentRunResult`` si le run se termine (approval en
        attente, rejet en boucle), sinon une chaîne ``str`` = message de
        continuation pour le LLM (résultats d'outils / erreurs)."""
        results: list[str] = []
        for step in plan.steps:
            action = step.action
            if action is None:  # étape multi-agents sans outil (non géré ici)
                results.append(f"[{step.task_id}] étape sans outil ignorée : {step.subtask}")
                continue
            decision = decide_action(action)

            if decision is Decision.REJECT:
                print_ = action.fingerprint()
                traces.append(ActionTrace(
                    tool=action.tool, args=action.args, decision=decision.value,
                    status="rejected", error="cible sensible ou règle dure",
                ))
                if print_ in rejected_prints:
                    # Anti-boucle : même action rejetée deux fois → arrêt.
                    return self._as_result(traces, budget, RunStatus.REJECTED_LOOP,
                                           answer="Action refusée par la politique de sécurité.")
                rejected_prints.add(print_)
                results.append(
                    f"[{step.task_id}] REJETÉ ({action.tool}) : règle de sécurité. "
                    "Reformule sans cet outil ni cette cible."
                )
                continue

            func = self._registry.get(action.tool)
            if func is None:
                results.append(
                    f"[{step.task_id}] ERREUR : outil inconnu '{action.tool}'. "
                    f"Outils valides : {', '.join(self._registry.tool_names())}."
                )
                traces.append(ActionTrace(
                    tool=action.tool, args=action.args, decision=decision.value,
                    status="error", error="outil inconnu",
                ))
                continue

            try:
                budget.consume_tool_call(action.tool)
            except BudgetExceededError:
                return self._finalize(traces, budget, RunStatus.BUDGET_EXHAUSTED)

            if decision is Decision.APPROVE:
                granted = self._approval_gateway(action) if self._approval_gateway else False
                if not granted:
                    traces.append(ActionTrace(
                        tool=action.tool, args=action.args, decision=decision.value,
                        status="awaiting_approval",
                    ))
                    return self._as_result(
                        traces, budget, RunStatus.PENDING_APPROVAL,
                        answer="En attente de validation humaine.",
                        awaiting_action=action,
                    )

            try:
                value = func(**action.args)
                results.append(f"[{step.task_id}] {action.tool} -> {self._summarize(value)}")
                traces.append(ActionTrace(
                    tool=action.tool, args=action.args, decision=decision.value,
                    status="done", result_summary=self._summarize(value),
                ))
            except Exception as exc:  # auto-correction : l'erreur retourne au LLM
                results.append(f"[{step.task_id}] ERREUR d'exécution ({action.tool}) : {exc}")
                traces.append(ActionTrace(
                    tool=action.tool, args=action.args, decision=decision.value,
                    status="error", error=str(exc),
                ))
        return "\n".join(results) if results else "Aucune action exécutée. Réponds à la question."

        # --- Finalisation -------------------------------------------------------------

    @staticmethod
    def _summarize(value: Any) -> str:
        """Résumé mono-ligne d'un résultat d'outil (aperçu, contexte plafonné)."""
        import json as _json

        if isinstance(value, (dict, list)):
            try:
                text = _json.dumps(value, ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                text = str(value)
        else:
            text = str(value)
        text = " ".join(text.split())
        if len(text) > _RESULT_SUMMARY_CHARS:
            return text[:_RESULT_SUMMARY_CHARS] + "… [tronqué]"
        return text

    @staticmethod
    def _as_result(
        traces: list[ActionTrace],
        budget: RunBudget,
        status: RunStatus,
        answer: str = "",
        awaiting_action: Action | None = None,
    ) -> AgentRunResult:
        return AgentRunResult(
            answer=answer,
            status=status,
            actions=list(traces),
            awaiting_action=awaiting_action,
            rounds_used=budget.snapshot().llm_rounds_used,
            tool_calls_used=budget.snapshot().tool_calls_used,
        )

    def _finalize(
        self,
        traces: list[ActionTrace],
        budget: RunBudget,
        status: RunStatus,
        answer: str = "",
    ) -> AgentRunResult:
        logger.info("Run agent terminé : statut=%s rounds=%s outils=%s",
                    status.value, budget.snapshot().llm_rounds_used,
                    budget.snapshot().tool_calls_used)
        return self._as_result(traces, budget, status, answer)
