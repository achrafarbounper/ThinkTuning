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
    PLAN_VALIDATION_FAILED,
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
from .roles import ROLE_ORDER, role_tools
from .plan_correct import PlanRejected, correct_plan
from .multi_run_fsm import MultiRunFSM, MultiRunState

logger = logging.getLogger("thinktuning.agent")

# --- Événements diffusés via ``on_event`` (voir also API/streaming) ---
EV_PLAN = "agent.plan"
EV_RESUMING = "agent.resuming"
EV_WORKER_START = "agent.worker.start"
EV_WORKER_TOOL = "agent.worker.tool"
EV_WORKER_THINKING = "agent.worker.thinking"
EV_WORKER_RESULT = "agent.worker.result"
EV_WORKER_ERROR = "agent.worker.error"
EV_WORKER_APPROVAL = "agent.worker.approval"
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
    max_rounds: Optional[int] = None,
    enable_thinking: bool = False,
) -> AgentCore:
    """Construit un AgentCore isolé pour un rôle (sous-ensemble d'outils).

    ``role_name`` est "lead" (aucun outil) ou un rôle spécialisé. Le prompt
    système est dérivé du rôle : sous-ensemble d'outils + préambule.
    ``max_rounds`` borne les rounds LLM du worker (quota réservé du
    ``BudgetPool`` global) — None = défaut historique de AgentCore.
    ``enable_thinking`` : mode « Réflexion » du worker (multi-agents) — le
    raisonnement est diffusé via ``on_thinking`` (événement
    ``agent.worker.thinking``) et archivé dans le résultat du worker.
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

    extra: Dict[str, Any] = {}
    if max_rounds is not None and max_rounds > 0:
        extra["max_rounds"] = int(max_rounds)
    if enable_thinking:
        extra["enable_thinking"] = True

    return AgentCore(
        llm_client,
        system_prompt=system_prompt,
        tools=tools,
        required_args=required,
        on_tool_forbidden=on_tool_forbidden,
        **extra,
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
        max_total_tool_calls: Optional[int] = None,
        enable_thinking: bool = False,
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
        # Mode « Réflexion » des workers (multi-agents). Le run peut le
        # surcharger par requête (``run(..., enable_thinking=...)``).
        self._enable_thinking = bool(enable_thinking)
        # Registre de REPRISE NATIVE : ``request_id`` -> snapshot du run interrompu
        # (plan, résultats workers déjà obtenus, tâche bloquée). La reprise ne
        # re-planifie PAS et ne re-dispatche QUE le worker bloqué (rounds LLM
        # minimaux). Borne mémoire : FIFO, entrées les plus anciennes évincées.
        self._resume_registry: Dict[str, Dict[str, Any]] = {}
        self._resume_registry_max = 32
        # Budget hiérarchique : plafond GLOBAL d'appels d'outils partagé par
        # les workers (None = désactivé, comportement V1 inchangé).
        from .budget_pool import build_budget_pool
        self._budget_pool = build_budget_pool(max_total_tool_calls, max_worker_rounds)
# --- Constitution des agents (lazy, DI-friendly) ----------------------

    def _make_agent(self, role_name: str, max_rounds: Optional[int] = None) -> Any:
        """Construit un agent (worker/lead) pour un rôle.

        ``max_rounds`` : quota de rounds réservé au worker via le
        ``BudgetPool`` (None = défaut). Via le DI ``role_builder``, le quota
        n'est pas injectable (signature ``(role_name)``) : la réserve du pool
        borne toujours l'ADMISSION du worker, indépendamment du builder.
        """
        if self._role_builder is not None:
            return self._role_builder(role_name)
        # Identité de paquet réel uniquement (plus de double identité d'import).
        from ..tools.tool_registry import REQUIRED_ARGS as _RA
        from ..tools.tool_registry import TOOLS as _T
        return build_role_agent(
            role_name, self._llm_client, _T, _RA,
            max_rounds=max_rounds,
            enable_thinking=self._thinking_enabled(),
        )

    def _thinking_enabled(self) -> bool:
        """Réflexion effective pour ce run (per-requête si fournie)."""
        override = getattr(self, "_run_thinking", None)
        return self._enable_thinking if override is None else bool(override)

    def _worker_prompt_for(self, task: PlanTask, prompt: str) -> str:
        """Prompt d'isolation stricte d'un worker (contexte résumé + sous-tâche)."""
        context_summary = truncate_context(prompt, self._context_chars)
        return (
            f"CONTEXTE GLOBAL (résumé) :\n{context_summary}\n\n"
            f"VOTRE SOUS-TÂCHE :\n{task.subtask}"
        )

    def _remember_resume(self, request_id: str, entry: Dict[str, Any]) -> None:
        """Enregistre un snapshot de reprise (FIFO borné)."""
        self._resume_registry[str(request_id)] = entry
        while len(self._resume_registry) > self._resume_registry_max:
            oldest = next(iter(self._resume_registry))
            self._resume_registry.pop(oldest, None)

    def _on_event(self, on_event, event_type: str, data: Dict[str, Any]):
        """Diffuse un événement (no-op si aucun callback)."""
        if on_event is not None:
            try:
                on_event(event_type, data)
            except Exception as exc:  # pragma: no cover
                logger.warning("on_event(%s) a échoué : %s", event_type, exc)

    # --- Plan --------------------------------------------------------------

    def _plan(self, prompt: str, on_event) -> Tuple[Optional[List[PlanTask]], Optional[Dict[str, Any]], Optional[str]]:
        """Planifie via le lead + valide. Retourne (tasks, error_dict, raw).

        Planner hybride : le LLM (lead) propose un plan, puis ``correct_plan``
        applique les règles métier DÉTERMINISTES (diagnostics → ops, shell
        whitelisté, SQL mutant rejeté…) avant la validation structurelle.
        """
        planner_prompt = (
            build_planner_prompt(prompt, self._roles, role_tools=role_tools())
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

        try:
            validation = validate_plan(
                raw or "",
                self._roles,
                max_roles=self._max_roles,
                preprocess=correct_plan,
            )
        except PlanRejected as exc:  # règle dure : SQL mutant / shell non whitelisté
            return None, {
                "error_code": PLAN_VALIDATION_FAILED,
                "message": exc.reason,
            }, raw
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
        """Exécute chaque sous-tâche sur son worker. Retourne (workers, unexecuted).

        La REPRISE d'un worker bloqué ne passe PAS par ici : ``run(...)`` en
        mode reprise appelle ``_run_worker(..., resume_request_id=...)``
        directement sur la tâche bloquée (rounds LLM minimaux).
        """
        specs = [(task, self._worker_prompt_for(task, prompt)) for task in tasks]

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

    def _run_worker(
        self,
        task: PlanTask,
        worker_prompt: str,
        on_event,
        resume_request_id: str | None = None,
    ) -> Dict[str, Any]:
        """Exécute une sous-tâche et normalise le résultat/erreur.

        ``resume_request_id`` : si fourni (reprise après validation humaine),
        le worker rejoue l'action approuvée au lieu de redemander un approve.
        """
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

        # Budget hiérarchique : réservation ferme dans le pool GLOBAL du run.
        # Quota refusé (pool épuisé) => worker refusé AVANT tout appel LLM.
        effective_rounds: Optional[int] = None
        if self._budget_pool is not None:
            granted = self._budget_pool.reserve(self._max_worker_rounds)
            if granted <= 0:
                return self._worker_error(
                    task, TOKEN_BUDGET_EXCEEDED,
                    "Budget global du run superviseur épuisé "
                    "(quota d'appels d'outils partagé).", started,
                )
            effective_rounds = min(granted, self._max_worker_rounds)

        agent = self._make_agent(task.role, max_rounds=effective_rounds)

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

        def thinking_hook(chunk):
            # Réflexion du worker (mode « Réflexion » multi-agents) — diffusée
            # en temps réel (agent.worker.thinking) et archivée par le flow store.
            self._on_event(on_event, EV_WORKER_THINKING, {
                "task_id": task.task_id, "role": task.role, "thinking": chunk,
            })

        return self._run_worker_core(
            agent, task, worker_prompt, tool_hook, forbidden_hook, started,
            on_event, resume_request_id=resume_request_id,
            on_thinking=thinking_hook,
        )

    def _run_worker_core(
        self, agent, task, worker_prompt, tool_hook, forbidden_hook, started, on_event,
        resume_request_id: str | None = None,
        on_thinking=None,
    ) -> Dict[str, Any]:
        thinking_parts: List[str] = []
        try:
            result = agent.run_detailed(
                worker_prompt, on_tool_event=tool_hook,
                on_thinking=on_thinking, resume_request_id=resume_request_id,
            )
        except Exception as exc:
            logger.error("Worker %s échoué : %s", task.task_id, exc)
            return self._worker_error(task, LLM_UNREACHABLE, str(exc), started)

        answer = getattr(result, "answer", str(result))
        thinking = getattr(result, "thinking", "")
        if thinking and not thinking_parts:
            thinking_parts.append(str(thinking))
        duration_ms = (time.perf_counter() - started) * 1000.0

        # Gate de décision : le worker s'est arrêté sur une action qui exige
        # une validation humaine (approve). On NE compte PAS ce worker comme
        # « ok » : l'outil n'a jamais tourné. Le request_id est remonté au
        # front (événement + contrat) pour afficher la carte Approuver/Refuser
        # puis relancer la sous-tâche avec resume_request_id.
        awaiting_id = getattr(agent, "awaiting_request_id", None)
        if awaiting_id:
            approval_obj = getattr(agent, "last_approval", None)
            approval = approval_obj.to_dict() if approval_obj is not None else {}
            worker = {
                "task_id": task.task_id,
                "role": task.role,
                "status": "awaiting_approval",
                "request_id": awaiting_id,
                "approval": approval,
                "message": answer,
                "duration_ms": round(duration_ms, 2),
            }
            self._on_event(on_event, EV_WORKER_APPROVAL, {
                "task_id": task.task_id,
                "role": task.role,
                "status": "awaiting_approval",
                "request_id": awaiting_id,
                "approval": approval,
                "message": (answer or "")[:300],
                "duration_ms": round(duration_ms, 2),
            })
            return worker

        worker = {
            "task_id": task.task_id,
            "role": task.role,
            "status": "ok",
            "result": answer,
            "duration_ms": round(duration_ms, 2),
            # Produit mais NON consommé en V1 (isolation stricte) — porte V2.
            "shareable_summary": (answer or "")[:300],
            "thinking": "\n".join(thinking_parts).strip(),
            "resumed": bool(resume_request_id),
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

    def run(
        self,
        prompt: str,
        on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        resume_request_id: Optional[str] = None,
        enable_thinking: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Exécute (ou REPREND) le cycle complet — contrat de sortie stable.

        Machine à états (``MultiRunFSM``) :
            planning → dispatch → waiting_workers
                → awaiting_approval (worker bloqué sur un approve)
                → resuming (reprise native) → synthesizing → completed

        En reprise (``resume_request_id``), le plan et les résultats des
        workers déjà exécutés proviennent du registre de reprise : le planning
        N'EST PAS relancé et seul le worker bloqué est re-dispatché — l'action
        approuvée est rejouée DANS le même worker (jamais dans le noyau
        mono-agent), puis la synthèse finale intègre l'ensemble des résultats.
        """
        started = time.perf_counter()
        if enable_thinking is not None:
            self._run_thinking = bool(enable_thinking)
        try:
            if not prompt or not str(prompt).strip():
                return {
                    "status": "error",
                    "error_code": SUPERVISOR_FAILED,
                    "message": "Prompt vide.",
                    "final_answer": "",
                    "plan": [], "workers": [], "unexecuted": [],
                }

            # --- Reprise native : AUCUNE re-planification (FSM : resuming) ------
            if resume_request_id:
                return self._resume(str(resume_request_id), on_event, started)

            # 1. Plan (planning → dispatch)
            fsm = MultiRunFSM.start()  # planning
            tasks, plan_error, raw_plan = self._plan(str(prompt), on_event)
            if tasks is None:
                fsm = fsm.transition(MultiRunState.ERROR)
                self._on_event(on_event, EV_ERROR, plan_error or {})
                return {
                    "status": "error",
                    "error_code": (plan_error or {}).get("error_code", SUPERVISOR_FAILED),
                    "message": (plan_error or {}).get("message", "Plan invalide."),
                    "final_answer": "",
                    "plan": [], "workers": [], "unexecuted": [],
                    "fsm_state": fsm.state.value,
                }
            fsm = fsm.transition(MultiRunState.DISPATCH)
            self._on_event(on_event, EV_PLAN, {"plan": [
                {"task_id": t.task_id, "role": t.role, "subtask": t.subtask}
                for t in tasks
            ]})

            # 2. Dispatch (dispatch → waiting_workers)
            workers, unexecuted = self._dispatch(tasks, str(prompt), on_event)
            fsm = fsm.transition(MultiRunState.WAITING_WORKERS)

            # Validation humaine requise : au moins une sous-tâche est bloquée
            # sur un approve. On interrompt AVANT la synthèse (une synthèse sans
            # le résultat du worker bloqué serait trompeuse) ; le front affiche
            # la carte Approuver/Refuser puis relance via resume_request_id —
            # la reprise passe par l'orchestrateur (FSM : awaiting_approval).
            pending_approvals = [w for w in workers if w.get("status") == "awaiting_approval"]

            if pending_approvals:
                fsm = fsm.transition(MultiRunState.AWAITING_APPROVAL)
                snapshot = {
                    "prompt": str(prompt),
                    "tasks": tasks,
                    "workers": workers,
                    "unexecuted": unexecuted,
                    "fsm": fsm,
                }
                for w in pending_approvals:
                    self._remember_resume(str(w.get("request_id")), {
                        **snapshot,
                        "resume_task": next(
                            (t for t in tasks if t.task_id == w.get("task_id")),
                            tasks[0],
                        ),
                    })
                return self._awaiting_outcome(
                    tasks, workers, unexecuted, pending_approvals, started, fsm,
                    on_event=on_event,
                )

            # 3. Synthèse (continueBroken) — garde FSM : JAMAIS si un worker
            # attend une validation (can_synthesize : waiting_workers only).
            final_answer, thinking = self._final_synthesis(
                str(prompt), workers, unexecuted, on_event
            )
            fsm = fsm.transition(MultiRunState.SYNTHESIZING)
            fsm = fsm.transition(MultiRunState.COMPLETED)

            outcome = {
                "status": "completed",
                "final_answer": final_answer,
                "plan": [
                    {"task_id": t.task_id, "role": t.role, "subtask": t.subtask}
                    for t in tasks
                ],
                "workers": workers,
                "unexecuted": unexecuted,
                "thinking": thinking,
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "fsm_state": fsm.state.value,
            }
            if self._budget_pool is not None:
                outcome["budget"] = self._budget_pool.snapshot()
            self._on_event(on_event, EV_DONE, {
                "status": outcome["status"],
                "answer": final_answer,
                "duration_ms": outcome["duration_ms"],
            })
            return outcome
        finally:
            self._run_thinking = None

    # --- Reprise native (FSM : awaiting_approval → resuming → …) -------------

    @staticmethod
    def _approval_row(request_id: str) -> Optional[Dict[str, Any]]:
        """Charge une demande d'approbation (store legacy partagé, import lazy)."""
        try:
            from core.approval_store import get_approval_store  # import local
            return get_approval_store().get(request_id)
        except Exception:  # pragma: no cover - store indisponible : refuse la reprise
            return None

    def _resume(
        self, resume_request_id: str, on_event, started: float
    ) -> Dict[str, Any]:
        """Reprise native : re-dispatch du worker bloqué, puis synthèse finale.

        Garde dures :
            - le snapshot de reprise doit exister (sinon erreur claire — JAMAIS
              de re-planification implicite) ;
            - la demande doit être APPROUVÉE (l'empreinte SHA-256 des arguments
              est revérifiée par le worker AgentCore au moment du rejeu) ;
            - l'action approuvée est rejouée dans le MÊME worker (même rôle,
              même sous-tâche) — jamais dans le noyau mono-agent.
        """
        entry = self._resume_registry.pop(resume_request_id, None)
        if entry is None:
            return {
                "status": "error",
                "error_code": SUPERVISOR_FAILED,
                "message": (
                    "Reprise impossible : aucune orchestration en attente pour "
                    f"la demande « {resume_request_id} » (session expirée ou "
                    "inconnue)."
                ),
                "final_answer": "",
                "plan": [], "workers": [], "unexecuted": [],
            }

        approval_row = self._approval_row(resume_request_id)
        if approval_row is None or approval_row.get("status") != "approved":
            return {
                "status": "error",
                "error_code": SUPERVISOR_FAILED,
                "message": (
                    f"Demande {resume_request_id} non approuvée : la reprise "
                    "exige une validation humaine préalable."
                ),
                "final_answer": "",
                "plan": [], "workers": [], "unexecuted": [],
            }

        fsm: MultiRunFSM = entry["fsm"]  # awaiting_approval
        task: PlanTask = entry["resume_task"]
        prompt: str = entry["prompt"]
        workers: List[Dict[str, Any]] = list(entry["workers"])
        unexecuted: List[Dict[str, Any]] = list(entry["unexecuted"])
        tasks: List[PlanTask] = list(entry["tasks"])

        fsm = fsm.transition(MultiRunState.RESUMING)
        self._on_event(on_event, EV_RESUMING, {
            "request_id": resume_request_id,
            "task_id": task.task_id,
            "role": task.role,
        })

        # Re-dispatch CIBLÉ : uniquement le worker bloqué, avec l'identifiant de
        # reprise (l'AgentCore rejoue l'action approuvée, empreinte validée).
        resumed = self._run_worker(
            task, self._worker_prompt_for(task, prompt), on_event,
            resume_request_id=resume_request_id,
        )
        resumed["resumed"] = True

        # Fusion : remplace l'ancien worker bloqué par son résultat rejoué.
        workers = [
            resumed if w.get("task_id") == task.task_id else w for w in workers
        ]
        unexecuted = [w for w in unexecuted if w.get("task_id") != task.task_id]
        if resumed.get("status") != "ok":
            unexecuted.append(resumed)

        if resumed.get("status") == "awaiting_approval":
            # La reprise a déclenché une NOUVELLE demande de validation
            # (action suivante du worker) : FSM resuming → awaiting_approval.
            fsm = fsm.transition(MultiRunState.AWAITING_APPROVAL)
            self._remember_resume(str(resumed.get("request_id")), {
                "prompt": prompt,
                "tasks": tasks,
                "workers": workers,
                "unexecuted": unexecuted,
                "fsm": fsm,
                "resume_task": next(
                    (t for t in tasks if t.task_id == resumed.get("task_id")), task
                ),
            })
            return self._awaiting_outcome(
                tasks, workers, unexecuted, [resumed], started, fsm,
                on_event=on_event,
            )

        # Synthèse finale : les résultats des workers (dont celui repris)
        # sont tous intégrés — synthèse cohérente, jamais partielle par oubli.
        fsm = fsm.transition(MultiRunState.SYNTHESIZING)
        final_answer, thinking = self._final_synthesis(
            prompt, workers, unexecuted, on_event
        )
        fsm = fsm.transition(MultiRunState.COMPLETED)

        outcome = {
            "status": "completed",
            "final_answer": final_answer,
            "plan": [
                {"task_id": t.task_id, "role": t.role, "subtask": t.subtask}
                for t in tasks
            ],
            "workers": workers,
            "unexecuted": unexecuted,
            "thinking": thinking,
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
            "fsm_state": fsm.state.value,
        }
        if self._budget_pool is not None:
            outcome["budget"] = self._budget_pool.snapshot()
        self._on_event(on_event, EV_DONE, {
            "status": outcome["status"],
            "answer": final_answer,
            "duration_ms": outcome["duration_ms"],
        })
        return outcome

    def _awaiting_outcome(
        self,
        tasks: List[PlanTask],
        workers: List[Dict[str, Any]],
        unexecuted: List[Dict[str, Any]],
        pending_approvals: List[Dict[str, Any]],
        started: float,
        fsm: MultiRunFSM,
        on_event=None,
    ) -> Dict[str, Any]:
        """Contrat de sortie « awaiting_approval » — AUCUNE synthèse émise."""
        listing = "; ".join(
            f"[{w['role']}] {w.get('approval', {}).get('tool', '?')} "
            f"({w.get('approval', {}).get('reason', 'validation requise')})"
            for w in pending_approvals
        )
        final_answer = (
            "Validation humaine requise avant de poursuivre. "
            f"Action(s) en attente : {listing}. "
            "Approuvez ou refusez dans l'interface pour relancer la sous-tâche."
        )
        outcome = {
            "status": "awaiting_approval",
            "final_answer": final_answer,
            "plan": [
                {"task_id": t.task_id, "role": t.role, "subtask": t.subtask}
                for t in tasks
            ],
            "workers": workers,
            "unexecuted": unexecuted,
            "pending_approvals": pending_approvals,
            "thinking": "",
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
            "fsm_state": fsm.state.value,
        }
        if self._budget_pool is not None:
            outcome["budget"] = self._budget_pool.snapshot()
        self._on_event(on_event, EV_DONE, {
            "status": outcome["status"],
            "answer": final_answer,
            "duration_ms": outcome["duration_ms"],
        })
        return outcome

    def _final_synthesis(
        self,
        prompt: str,
        workers: List[Dict[str, Any]],
        unexecuted: List[Dict[str, Any]],
        on_event,
    ) -> Tuple[str, str]:
        """Synthèse finale (continueBroken) — dégradée mais cohérente si échec.

        Retourne ``(final_answer, thinking)`` ; le ``thinking`` agrège les
        traces de réflexion des workers (mode « Réflexion » multi-agents).
        """
        thinking = "\n\n".join(
            str(w.get("thinking") or "") for w in workers if w.get("thinking")
        ).strip()
        if any(w.get("status") == "ok" for w in workers):
            try:
                return self._synthesize(prompt, workers, unexecuted, on_event), thinking
            except WorkerError as exc:
                return (
                    "La synthèse finale n'a pas pu être produite. "
                    f"Résultats partiels : {len(workers)} exécuté(s), "
                    f"{len(unexecuted)} en échec."
                ), thinking
        return (
            "Aucune sous-tâche n'a pu être exécutée. "
            "Détails : " + json.dumps(unexecuted, ensure_ascii=False)
        ), thinking
