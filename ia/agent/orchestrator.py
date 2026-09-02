"""Orchestration multi-agents — superviseur (plan → dispatch → synthèse).

Cycle de vie d'un run ``MultiAgentCoordinator.run`` :
  1. PLAN     : le LEAD (prompt superviseur) décompose la demande en
                sous-tâches assignées aux rôles (JSON). Le plan est ensuite
                validé DÉTERMINISTIQUEMENT par ``plan_validator`` — un plan
                invalide ⇒ abort global (pas de dispersion).
  2. DISPATCH : chaque sous-tâche est exécutée par le worker de son rôle,
                avec ISOLATION STRICTE du contexte (chacun reçoit le contexte
                global résumé + SA sous-tâche, RIEN d'autre). Un agent est
                créé PAR appel worker : aucun état partagé croisé, donc sûr
                en threads (parallèle optionnel).
  3. SYNTHÈSE : le LEAD (sans outil) agrège les résultats des workers en une
                réponse finale, en signalant explicitement chaque sous-tâche
                non exécutée (continueBroken, jamais de replanification en V1).

Erreurs : granularité fine dans errors.py mais UNIQUEMENT trois buckets de
comportement (ok / failed / abort) — on ne sur-spécialise pas par code.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from .agent_core import AgentCore
from .errors import (
    LLM_UNREACHABLE,
    SUPERVISOR_FAILED,
    SYNTHESIS_FAILED,
    TOOL_NOT_ALLOWED,
    TOKEN_BUDGET_EXCEEDED,
)
from .plan_validator import PlanTask, validate_plan
from .prompts import (
    build_planner_prompt,
    build_synthesis_prompt,
    build_synthesis_system,
    truncate_context,
)
from .roles import ROLE_ORDER

logger = logging.getLogger("thinktuning.agent")

# --- Événements diffusés via ``on_event`` (voir also API/streaming) ---
EV_PLAN = "agent.plan"
EV_WORKER_START = "agent.worker.start"
EV_WORKER_TOOL = "agent.worker.tool"
EV_WORKER_RESULT = "agent.worker.result"
EV_WORKER_ERROR = "agent.worker.error"
EV_SYNTHESIZING = "agent.synthesizing"
EV_DONE = "agent.done"
EV_ERROR = "agent.error"


def _make_task_id() -> str:
    return "task-" + uuid.uuid4().hex[:8]


class WorkerError(Exception):
    """Erreur d'exécution d'un worker, portant un code d'erreur (bucket)."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code
        self.message = message
def build_role_agent(
    role_name: str,
    llm_client,
    tools_registry: Dict[str, Any],
    required_args_registry: Dict[str, List[str]],
    on_tool_forbidden: Optional[Callable[[str, str], None]] = None,
) -> AgentCore:
    """Construit un AgentCore isolé pour un rôle (sous-ensemble d'outils).

    ``role_name`` est "lead" (aucun outil) ou un rôle spécialisé. Le prompt
    système est dérivé du rôle : sous-ensemble d'outils + préambule.
    """
    from .roles import resolve_role_tools
    from .system_prompt import build_system_prompt

    if role_name == "lead":
        tools = {}
        required = {}
        system_prompt = build_synthesis_system()
    else:
        tools = resolve_role_tools(role_name, tools_registry)
        required = {
            name: required_args_registry[name]
            for name in tools if name in required_args_registry
        }
        system_prompt = build_system_prompt(tools, required)

    return AgentCore(
        llm_client,
        system_prompt=system_prompt,
        tools=tools,
        required_args=required,
        on_tool_forbidden=on_tool_forbidden,
    )


class MultiAgentCoordinator:
    """Superviseur multi-agents : plan déterministe → dispatch isolé → synthèse.

    Paramètres :
      - ``role_builder`` (DI, testable) : callable ``(role_name) -> agent``
        exposant ``run_detailed``. Par défaut ``build_role_agent``.
      - ``roles`` : noms des rôles spécialisés (pour la validation).
      - ``max_worker_rounds`` / ``max_worker_tokens`` : budgets du worker.
    """

    def __init__(
        self,
        llm_client,
        role_builder: Optional[Callable[..., Any]] = None,
        roles: Optional[List[str]] = None,
        lead_llm_client=None,
        parallel: bool = False,
        max_workers: int = 4,
        max_roles: int = 5,
        max_worker_rounds: int = 6,
        max_worker_tokens: int = 1200,
        context_chars: int = 2400,
    ):
        self._llm_client = llm_client
        self._lead_llm_client = lead_llm_client or llm_client
        self._roles = list(roles) if roles else list(ROLE_ORDER)
        self._role_builder = role_builder
        self._parallel = parallel
        self._max_workers = max_workers
        self._max_roles = max_roles
        self._max_worker_rounds = max_worker_rounds
        self._max_worker_tokens = max_worker_tokens
        self._context_chars = context_chars
        self._lead_subject = None
# --- Constitution des agents (lazy, DI-friendly) ----------------------

    def _make_agent(self, role_name: str) -> Any:
        """Construit un agent (worker/lead) pour un rôle."""
        if self._role_builder is not None:
            return self._role_builder(role_name)
        try:  # paquet « ia.tools »
            from ..tools.tool_registry import REQUIRED_ARGS as _RA
            from ..tools.tool_registry import TOOLS as _T
        except ImportError:  # racine « tools » (core/agent_cache.py)
            from tools.tool_registry import REQUIRED_ARGS as _RA
            from tools.tool_registry import TOOLS as _T
        return build_role_agent(
            role_name, self._llm_client, _T, _RA
        )

    def _on_event(self, on_event, event_type: str, data: Dict[str, Any]):
        """Diffuse un événement (no-op si aucun callback)."""
        if on_event is not None:
            try:
                on_event(event_type, data)
            except Exception as exc:  # pragma: no cover
                logger.warning("on_event(%s) a échoué : %s", event_type, exc)

    # --- Plan --------------------------------------------------------------

    def _plan(self, prompt: str, on_event) -> Tuple[Optional[List[PlanTask]], Optional[Dict[str, Any]], Optional[str]]:
        """Planifie via le lead + valide. Retourne (tasks, error_dict, raw)."""
        planner_prompt = (
            build_planner_prompt(prompt, self._roles)
            + "\n\nRéponds avec un unique JSON valide."
        )
        lead = self._build_lead()
        raw = None
        try:
            result = lead.run_detailed(
                planner_prompt, on_thinking=None, on_tool_event=None
            )
            raw = result.answer if hasattr(result, "answer") else str(result)
        except Exception as exc:  # pragma: no cover
            logger.error("Planification échouée : %s", exc)
            return None, {
                "error_code": SUPERVISOR_FAILED,
                "message": f"Le superviseur n'a pas pu planifier : {exc}",
            }, raw

        validation = validate_plan(
            raw or "", self._roles, max_roles=self._max_roles
        )
        if not validation.ok:
            return None, {
                "error_code": validation.error_code,
                "message": validation.message,
            }, raw
        return validation.tasks, None, raw

    def _build_lead(self):
        if self._lead_subject is not None:
            return self._lead_subject
        self._lead_subject = self._make_agent("lead")
        return self._lead_subject
# --- Dispatch ----------------------------------------------------------

    def _dispatch(self, tasks: List[PlanTask], prompt: str, on_event) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Exécute chaque sous-tâche sur son worker. Retourne (workers, unexecuted)."""
        context_summary = truncate_context(prompt, self._context_chars)
        specs = []
        for task in tasks:
            worker_prompt = (
                f"CONTEXTE GLOBAL (résumé) :\n{context_summary}\n\n"
                f"VOTRE SOUS-TÂCHE :\n{task.subtask}"
            )
            specs.append((task, worker_prompt))

        workers: List[Dict[str, Any]] = []
        unexecuted: List[Dict[str, Any]] = []

        def run_one(spec):
            task, worker_prompt = spec
            return self._run_worker(task, worker_prompt, on_event)

        if self._parallel and len(specs) > 1:
            with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
                results = list(pool.map(run_one, specs))
        else:
            results = [run_one(spec) for spec in specs]

        for res in results:
            workers.append(res)
            if res["status"] != "ok":
                unexecuted.append(res)
        return workers, unexecuted

    def _run_worker(self, task: PlanTask, worker_prompt: str, on_event) -> Dict[str, Any]:
        """Exécute une sous-tâche et normalise le résultat/erreur."""
        started = time.perf_counter()
        self._on_event(on_event, EV_WORKER_START, {
            "task_id": task.task_id, "role": task.role,
        })

        # Budget de jetons (estimation déterministe) avant tout appel LLM.
        from .context import estimate_tokens
        if estimate_tokens(worker_prompt) > self._max_worker_tokens:
            return self._worker_error(
                task, TOKEN_BUDGET_EXCEEDED,
                "Budget de jetons dépassé avant exécution.", started,
            )

        agent = self._make_agent(task.role)

        def tool_hook(payload):
            self._on_event(on_event, EV_WORKER_TOOL, {
                "task_id": task.task_id, "role": task.role, **payload,
            })

        def forbidden_hook(tool, message):
            self._on_event(on_event, EV_WORKER_ERROR, {
                "task_id": task.task_id,
                "role": task.role,
                "error_code": TOOL_NOT_ALLOWED,
                "message": message,
            })

        return self._run_worker_core(
            agent, task, worker_prompt, tool_hook, forbidden_hook, started, on_event
        )

    def _run_worker_core(self, agent, task, worker_prompt, tool_hook, forbidden_hook, started, on_event) -> Dict[str, Any]:
        try:
            result = agent.run_detailed(
                worker_prompt, on_tool_event=tool_hook, on_thinking=None
            )
        except Exception as exc:
            logger.error("Worker %s échoué : %s", task.task_id, exc)
            return self._worker_error(task, LLM_UNREACHABLE, str(exc), started)

        answer = getattr(result, "answer", str(result))
        duration_ms = (time.perf_counter() - started) * 1000.0
        worker = {
            "task_id": task.task_id,
            "role": task.role,
            "status": "ok",
            "result": answer,
            "duration_ms": round(duration_ms, 2),
            # Produit mais NON consommé en V1 (isolation stricte) — porte V2.
            "shareable_summary": (answer or "")[:300],
        }
        self._on_event(on_event, EV_WORKER_RESULT, {
            "task_id": task.task_id, "role": task.role, "status": "ok",
            "summary": (answer or "")[:300], "duration_ms": round(duration_ms, 2),
        })
        return worker

    def _worker_error(self, task, error_code: str, message: str, started) -> Dict[str, Any]:
        duration_ms = (time.perf_counter() - started) * 1000.0
        return {
            "task_id": task.task_id,
            "role": task.role,
            "status": "error",
            "error_code": error_code,
            "message": message,
            "duration_ms": round(duration_ms, 2),
        }

    # --- Synthèse ----------------------------------------------------------

    def _synthesize(self, prompt: str, workers: List[Dict[str, Any]], unexecuted: List[Dict[str, Any]], on_event) -> str:
        self._on_event(on_event, EV_SYNTHESIZING, {"worker_errors": len(unexecuted)})
        synth_prompt = build_synthesis_prompt(prompt, workers, unexecuted)
        lead = self._build_lead()
        try:
            result = lead.run_detailed(synth_prompt, on_thinking=None, on_tool_event=None)
        except Exception as exc:
            logger.error("Synthèse échouée : %s", exc)
            raise WorkerError(SYNTHESIS_FAILED, f"La synthèse finale a échoué : {exc}") from exc
        return str(getattr(result, "answer", str(result)))

    # --- API publique ------------------------------------------------------

    def run(self, prompt: str, on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None) -> Dict[str, Any]:
        """Exécute le cycle complet et rend le contrat de sortie stable."""
        started = time.perf_counter()
        if not prompt or not str(prompt).strip():
            return {
                "status": "error",
                "error_code": SUPERVISOR_FAILED,
                "message": "Prompt vide.",
                "final_answer": "",
                "plan": [], "workers": [], "unexecuted": [],
            }

        # 1. Plan
        tasks, plan_error, raw_plan = self._plan(str(prompt), on_event)
        if tasks is None:
            self._on_event(on_event, EV_ERROR, plan_error or {})
            return {
                "status": "error",
                "error_code": (plan_error or {}).get("error_code", SUPERVISOR_FAILED),
                "message": (plan_error or {}).get("message", "Plan invalide."),
                "final_answer": "",
                "plan": [], "workers": [], "unexecuted": [],
            }
        self._on_event(on_event, EV_PLAN, {"plan": [
            {"task_id": t.task_id, "role": t.role, "subtask": t.subtask}
            for t in tasks
        ]})

        # 2. Dispatch
        workers, unexecuted = self._dispatch(tasks, str(prompt), on_event)

        # 3. Synthèse (continueBroken) — tentée si au moins un ok.
        if any(w["status"] == "ok" for w in workers):
            try:
                final_answer = self._synthesize(str(prompt), workers, unexecuted, on_event)
            except WorkerError as exc:
                final_answer = (
                    "La synthèse finale n'a pas pu être produite. "
                    f"Résultats partiels : {len(workers)} exécuté(s), "
                    f"{len(unexecuted)} en échec."
                )
        else:
            final_answer = (
                "Aucune sous-tâche n'a pu être exécutée. "
                "Détails : " + json.dumps(unexecuted, ensure_ascii=False)
            )

        outcome = {
            "status": "completed",
            "final_answer": final_answer,
            "plan": [
                {"task_id": t.task_id, "role": t.role, "subtask": t.subtask}
                for t in tasks
            ],
            "workers": workers,
            "unexecuted": unexecuted,
            "thinking": "",
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
        }
        self._on_event(on_event, EV_DONE, {
            "status": outcome["status"],
            "answer": final_answer,
            "duration_ms": outcome["duration_ms"],
        })
        return outcome