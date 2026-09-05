"""Orchestration multi-agents — superviseur (plan → dispatch → synthèse).

Cycle de vie d'un run ``MultiAgentCoordinator.run`` :
  0. INTENT   : (optionnel, Approche B) le classifieur chat/action est
                consulté AVANT la planification ; l'intention GLOBALE est
                stampée sur chaque sous-tâche puis appliquée au dispatch
                (politique par rôle, ``roles.INTENT_POLICY``) : un rôle hors
                périmètre est FILTRÉ (worker « ignored », ni appel LLM ni
                outil). Si TOUTES les sous-tâches sont filtrées — ou plan
                vide sur intention « chat » — le superviseur répond
                directement en mode conversationnel (FSM : FALLBACK_CHAT),
                repli MANDATOIRE contre tout silence utilisateur.
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
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from .agent_core import AgentCore
from .errors import (
    LLM_UNREACHABLE,
    PLAN_EMPTY,
    PLAN_VALIDATION_FAILED,
    SUPERVISOR_FAILED,
    SYNTHESIS_FAILED,
    TOOL_NOT_ALLOWED,
    TOOL_PROPOSAL_LIMITED,
    TOOL_PROPOSAL_REJECTED,
    TOKEN_BUDGET_EXCEEDED,
)
from .plan_validator import PlanTask, validate_plan
from .prompts import (
    build_fallback_system,
    build_planner_prompt,
    build_synthesis_prompt,
    build_synthesis_system,
    truncate_context,
)
from .roles import ROLE_ORDER, intent_decision_for, role_tools
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
# --- Intention (Approche B : classification globale + filtrage par rôle) ---
EV_INTENT = "agent.intent"                  # intention globale détectée (superviseur)
EV_WORKER_SKIPPED = "agent.worker.skipped"  # worker filtré par la politique d'intention
EV_FALLBACK = "agent.fallback"              # repli conversationnel (aucun worker exécuté)
# --- Tools personnalisés (SCRUM-99 : propositions du planner) ---------------
EV_TOOL_PROPOSED = "agent.tool.proposed"    # le planner propose un nouveau tool
EV_TOOL_REVIEWED = "agent.tool.reviewed"    # verdict de la relecture reviewer


def _make_task_id() -> str:
    return "task-" + uuid.uuid4().hex[:8]


def _dynamic_tools_enabled() -> bool:
    """Interrupteur ENV global ``USE_DYNAMIC_TOOLS`` (défaut : activé).

    ``USE_DYNAMIC_TOOLS=false`` (0/no) ⇒ mode legacy STRICT : aucune
    proposition de tool n'est sollicitée ni traitée, quel que soit le
    paramétrage du coordinateur (comportement historique garanti).
    """
    return str(os.environ.get("USE_DYNAMIC_TOOLS", "true")).strip().lower() not in (
        "0", "false", "no", "off",
    )


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
    intent: str = "action",
    intent_confidence: float = 0.0,
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

    agent = AgentCore(
        llm_client,
        system_prompt=system_prompt,
        tools=tools,
        required_args=required,
        on_tool_forbidden=on_tool_forbidden,
        **extra,
    )
    # Observabilité d'intention (Approche B) : l'agent porte l'intention
    # GLOBALE stampée par l'orchestrateur. Lecture seule : la boucle LLM de
    # ``AgentCore`` n'est PAS modifiée — le filtrage reste au niveau de
    # l'orchestrateur (``roles.intent_decision_for``).
    agent.intent = intent
    agent.intent_confidence = intent_confidence
    return agent


class MultiAgentCoordinator:
    """Superviseur multi-agents : plan déterministe → dispatch isolé → synthèse.

    Paramètres :
      - ``role_builder`` (DI, testable) : callable ``(role_name) -> agent``
        exposant ``run_detailed``. Par défaut ``build_role_agent``.
      - ``roles`` : noms des rôles spécialisés (pour la validation).
      - ``max_worker_rounds`` / ``max_worker_tokens`` : budgets du worker.
      - ``intent_classifier`` : classifieur chat/action optionnel (Approche B).
        Injecté ⇒ classification GLOBALE avant planification + filtrage LOCAL
        par rôle au dispatch + repli conversationnel (FALLBACK_CHAT). Absent ⇒
        comportement historique strictement inchangé (tous les workers
        s'exécutent, aucun filtrage, aucun repli).
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
        intent_classifier=None,
        tool_registry=None,
        enable_tool_proposals: bool = False,
        max_tool_proposals_per_plan: int = 1,
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
        # Classifieur d'intention (chat/action, Phase 4) — Approche B :
        # classification GLOBALE au superviseur AVANT planification, filtrage
        # LOCAL par rôle au dispatch, repli conversationnel FALLBACK_CHAT si
        # tout le plan est hors périmètre. ``None`` = fonctionnalité désactivée
        # (comportement V1 strictement inchangé).
        self._intent_classifier = intent_classifier
        self._intent_active = intent_classifier is not None
        # SCRUM-99 : tools personnalisés. ``tool_registry`` (ToolRegistry
        # injectée) enrichit l'outillage des workers (merged_registry) ;
        # ``enable_tool_proposals`` active le pipeline proposer → relire du
        # planner. FAIL-CLOSED : l'orchestrateur n'enregistre JAMAIS un tool
        # lui-même — l'enregistrement est une décision humaine (API).
        # ``USE_DYNAMIC_TOOLS=false`` ⇒ strict legacy (tout désactivé).
        self._tool_registry = tool_registry
        self._tool_proposals_requested = bool(enable_tool_proposals)
        self._enable_tool_proposals = (
            self._tool_proposals_requested and _dynamic_tools_enabled()
        )
        self._max_tool_proposals = max(0, int(max_tool_proposals_per_plan))
        self._plan_tool_proposals: List[Dict[str, Any]] = []
        self._plan_tool_notes: List[Dict[str, Any]] = []
        self._fallback_subject = None  # agent de repli conversationnel (lazy)
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

    def _make_agent(
        self,
        role_name: str,
        max_rounds: Optional[int] = None,
        intent: str = "action",
        intent_confidence: float = 0.0,
    ) -> Any:
        """Construit un agent (worker/lead) pour un rôle.

        ``max_rounds`` : quota de rounds réservé au worker via le
        ``BudgetPool`` (None = défaut). Via le DI ``role_builder``, le quota
        n'est pas injectable (signature ``(role_name)``) : la réserve du pool
        borne toujours l'ADMISSION du worker, indépendamment du builder.
        ``intent`` / ``intent_confidence`` : intention GLOBALE transmise au
        builder PAR DÉFAUT (observabilité sur l'agent réel) ; le DI
        ``role_builder`` n'a pas besoin de la connaître — le filtrage reste
        côté orchestrateur.
        """
        if self._role_builder is not None:
            return self._role_builder(role_name)
        # Registry injectée (SCRUM-99) : vue FUSIONNÉE (natifs + dynamiques
        # enregistrés au fil de l'eau). Sinon dicts statiques historiques.
        if self._tool_registry is not None:
            tools_map, required_map = self._tool_registry.merged_registry()
        else:
            # Identité de paquet réel uniquement (plus de double identité d'import).
            from ..tools.tool_registry import REQUIRED_ARGS as _RA
            from ..tools.tool_registry import TOOLS as _T
            tools_map, required_map = _T, _RA
        return build_role_agent(
            role_name, self._llm_client, tools_map, required_map,
            max_rounds=max_rounds,
            enable_thinking=self._thinking_enabled(),
            intent=intent,
            intent_confidence=intent_confidence,
        )

    def _thinking_enabled(self) -> bool:
        """Réflexion effective pour ce run (per-requête si fournie)."""
        override = getattr(self, "_run_thinking", None)
        return self._enable_thinking if override is None else bool(override)

    def _worker_prompt_for(self, task: PlanTask, prompt: str) -> str:
        """Prompt d'isolation stricte d'un worker (contexte résumé + sous-tâche).

        Approche B : l'intention GLOBALE est rappelée au worker (guidage du
        prompt, la boucle LLM n'est PAS modifiée). ``action`` ⇒ exécution
        outillée attendue ; ``chat`` ⇒ réponse conversationnelle (outil en
        dernier recours) pour les rôles pass_through/chat_first qui passent
        le filtre.
        """
        context_summary = truncate_context(prompt, self._context_chars)
        intent_note = ""
        if self._intent_active:
            if task.intent == "action":
                intent_note = (
                    f"\n\nINTENTION DÉTECTÉE : action (confiance "
                    f"{task.intent_confidence:.2f}) — la demande exige une "
                    "exécution réelle : utilisez vos outils si nécessaire et "
                    "fondez votre réponse sur leurs résultats."
                )
            else:
                intent_note = (
                    f"\n\nINTENTION DÉTECTÉE : chat (confiance "
                    f"{task.intent_confidence:.2f}) — réponse conversationnelle "
                    "attendue : n'utilisez un outil qu'en dernier recours."
                )
        return (
            f"CONTEXTE GLOBAL (résumé) :\n{context_summary}\n\n"
            f"VOTRE SOUS-TÂCHE :\n{task.subtask}"
            f"{intent_note}"
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

    # --- Intention (Approche B : classification globale au superviseur) -----

    def _classify_intent(
        self, prompt: str, on_event,
    ) -> Optional[Dict[str, Any]]:
        """Classifie l'intention GLOBALE du prompt (chat/action).

        Retourne ``{"intent", "confidence", "engine"}`` ou ``None`` si aucun
        classifieur n'est injecté (fonctionnalité désactivée). DÉFENSIF : tout
        échec du classifieur retombe sur ``action`` (comportement historique —
        les workers s'exécutent) : une panne de classification ne doit JAMAIS
        casser ni détourner un run.
        """
        if not self._intent_active:
            return None
        meta = {"intent": "action", "confidence": 0.0, "engine": "error"}
        try:
            results = self._intent_classifier.predict([prompt])
            if results:
                first = results[0]
                label = str(getattr(first, "label", "") or "").strip().lower()
                if label not in ("chat", "action"):
                    label = "action"  # étiquette inattendue : comportement sûr
                meta = {
                    "intent": label,
                    "confidence": float(getattr(first, "confidence", 0.0) or 0.0),
                    "engine": str(
                        getattr(self._intent_classifier, "engine", "") or ""
                    ),
                }
        except Exception as exc:  # pragma: no cover - chemin défensif
            logger.warning(
                "Classification d'intention échouée (repli « action ») : %s", exc,
            )
        logger.info(
            "intent_global intent=%s confidence=%.3f engine=%s",
            meta["intent"], meta["confidence"], meta["engine"],
        )
        self._on_event(on_event, EV_INTENT, dict(meta))
        return meta

    def _stamp_intent(
        self, tasks: List[PlanTask], intent_meta: Optional[Dict[str, Any]],
    ) -> None:
        """Stamp l'intention GLOBALE sur chaque sous-tâche (propagation)."""
        if not self._intent_active or intent_meta is None:
            return
        for task in tasks:
            task.intent = intent_meta.get("intent", "action")
            task.intent_confidence = float(intent_meta.get("confidence", 0.0))

    @staticmethod
    def _plan_dicts(tasks: List[PlanTask]) -> List[Dict[str, Any]]:
        """Plan sérialisé (contrat stable : task_id/role/subtask + intent)."""
        return [
            {
                "task_id": t.task_id,
                "role": t.role,
                "subtask": t.subtask,
                "intent": t.intent,
            }
            for t in tasks
        ]

    # --- Plan --------------------------------------------------------------

    def _plan(
        self, prompt: str, on_event, intent_meta: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[List[PlanTask]], Optional[Dict[str, Any]], Optional[str]]:
        """Planifie via le lead + valide. Retourne (tasks, error_dict, raw).

        Planner hybride : le LLM (lead) propose un plan, puis ``correct_plan``
        applique les règles métier DÉTERMINISTES (diagnostics → ops, shell
        whitelisté, SQL mutant rejeté…) avant la validation structurelle.
        ``intent_meta`` : intention globale (Approche B) — le planner adapte
        son plan : intention « chat » ⇒ sous-tâches uniquement si une action
        réelle est clairement nécessaire (sinon plan vide → repli direct).
        """
        intent = (intent_meta or {}).get("intent")
        confidence = (intent_meta or {}).get("confidence")
        self._plan_tool_proposals = []
        self._plan_tool_notes = []
        planner_prompt = (
            build_planner_prompt(
                prompt, self._roles, role_tools=role_tools(),
                intent=intent, intent_confidence=confidence,
                allow_tool_proposals=self._enable_tool_proposals,
                max_tool_proposals=self._max_tool_proposals,
            )
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
                max_tool_proposals=self._max_tool_proposals,
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
        # SCRUM-99 : propositions de tools extraites du plan (pseudo-rôle
        # propose_tool) — traitées par le pipeline de relecture (run()).
        self._plan_tool_proposals = list(validation.tool_proposals)
        self._plan_tool_notes = list(validation.tool_proposal_notes)
        return validation.tasks, None, raw

    # --- Tools personnalisés : pipeline proposer → relire (SCRUM-99) --------

    def _handle_tool_proposals(
        self, on_event,
    ) -> List[Dict[str, Any]]:
        """Traite les propositions de tools extraites du plan (SCRUM-99).

        Par proposition :
          1. ``agent.tool.proposed`` (définition design-time complète) ;
          2. RELUE par le worker « reviewer » (aucun outil : il JUGE, il ne
             peut rien exécuter ni contourner) — verdict JSON strict ;
          3. verdict « approve » → ``agent.tool.reviewed`` (approved) : la
             proposition est RETOURNÉE à l'humain dans l'outcome ;
             l'enregistrement effectif (code + registry.add_tool) reste une
             décision HUMAINE via l'API — le planner ne s'auto-équipe JAMAIS ;
          4. verdict « reject » → ``agent.tool.reviewed`` (rejected) + code
             ``ToolProposalRejected`` (traçabilité : le plan continue SANS) ;
          5. notes du validateur (plafond / duplicata / définition invalide)
             → ``ToolProposalLimited`` ou ``ToolProposalRejected``.
        """
        results: List[Dict[str, Any]] = []
        proposals = list(self._plan_tool_proposals)
        notes = list(self._plan_tool_notes)
        if not proposals and not notes:
            return results
        if not self._enable_tool_proposals:
            # Pipeline désactivé (flag ou USE_DYNAMIC_TOOLS=false) : les
            # propositions extraites sont JETÉES (strict legacy, zéro effet).
            logger.info(
                "tool_proposals ignorées (pipeline désactivé) : %d proposition(s)",
                len(proposals),
            )
            return []
        for note in notes:
            reason = str(note.get("reason", ""))
            code = (
                TOOL_PROPOSAL_LIMITED
                if "plafond" in reason else TOOL_PROPOSAL_REJECTED
            )
            entry = {
                "name": note.get("name", ""),
                "task_id": note.get("task_id", ""),
                "status": "rejected",
                "error_code": code,
                "reason": reason,
            }
            results.append(entry)
            self._on_event(on_event, EV_TOOL_REVIEWED, {**entry, "stage": "validation"})
            logger.info(
                "tool_proposal_validation name=%s code=%s reason=%s",
                entry["name"], code, reason,
            )
        for proposal in proposals:
            name = str(proposal.get("name", ""))
            self._on_event(on_event, EV_TOOL_PROPOSED, {
                "name": name,
                "task_id": proposal.get("task_id", ""),
                "definition": proposal,
            })
            approved, reason = self._review_tool_proposal(proposal)
            entry: Dict[str, Any] = {
                "name": name,
                "task_id": proposal.get("task_id", ""),
                "definition": proposal,
                "status": "approved" if approved else "rejected",
                "reason": reason,
            }
            if not approved:
                entry["error_code"] = TOOL_PROPOSAL_REJECTED
            results.append(entry)
            self._on_event(on_event, EV_TOOL_REVIEWED, {
                "name": name,
                "task_id": proposal.get("task_id", ""),
                "decision": "approved" if approved else "rejected",
                "reason": reason,
            })
            logger.info(
                "tool_proposal name=%s decision=%s reason=%s",
                name, "approved" if approved else "rejected", reason,
            )
        return results

    def _review_tool_proposal(self, proposal: Dict[str, Any]) -> Tuple[bool, str]:
        """Relit une proposition via le worker « reviewer » (aucun outil).

        Retourne ``(approved, raison)``. TOUT échec (agent indisponible,
        verdict illisible ou inattendu) ⇒ ``(False, ...)`` : fail-closed.
        """
        review_prompt = (
            "Vous êtes le relecteur sécurité d'une PROPOSITION d'outil pour "
            "le registre dynamique. Analysez la définition ci-dessous.\n\n"
            "DÉFINITION PROPOSÉE (standard thinktuning.tool/v1) :\n"
            + json.dumps(proposal, ensure_ascii=False, indent=2, default=str)
            + "\n\nCritères de relecture :\n"
            "- nécessité réelle (capacité réellement manquante) et "
            "description claire ;\n"
            "- paramètres typés et nécessaires, pas de sur-privilège ;\n"
            "- aucun effet destructeur, aucune exécution de code arbitraire, "
            "aucun accès non filtré (shell libre, SQL mutant, réseau non "
            "borné) ;\n"
            "- nom correct (minuscules/underscores, pas un outil existant).\n\n"
            'Répondez UNIQUEMENT avec un JSON : {"verdict": "approve" ou '
            '"reject", "reason": "justification courte en français"}.'
        )
        try:
            agent = self._make_agent("reviewer")
            result = agent.run_detailed(
                review_prompt, on_thinking=None, on_tool_event=None,
            )
            raw = str(getattr(result, "answer", str(result)) or "")
        except Exception as exc:  # pragma: no cover - chemin défensif
            logger.warning("Review de tool impossible : %s", exc)
            return False, f"review indisponible : {exc}"
        from .json_parser import extract_json_blocks
        verdict, reason = "", ""
        for block in extract_json_blocks(raw or ""):
            if isinstance(block, dict) and "verdict" in block:
                verdict = str(block.get("verdict", "")).strip().lower()
                reason = str(block.get("reason", "") or "").strip()
                break
        if verdict == "approve":
            return True, reason or "verdict reviewer : approve"
        if verdict == "reject":
            return False, reason or "verdict reviewer : reject"
        return False, f"verdict reviewer illisible : {(raw or '')[:200]}"

    def _build_lead(self):
        if self._lead_subject is not None:
            return self._lead_subject
        self._lead_subject = self._make_agent("lead")
        return self._lead_subject
# --- Dispatch ----------------------------------------------------------

    def _dispatch(self, tasks: List[PlanTask], prompt: str, on_event) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Exécute chaque sous-tâche sur son worker. Retourne (workers, unexecuted).

        Approche B : filtrage LOCAL par rôle (politique d'intention) AVANT
        toute construction d'agent — un worker « ignored » ne coûte NI appel
        LLM NI appel d'outil, et n'entre JAMAIS dans le circuit d'approbation.
        Les workers filtrés restent TRACÉS dans le contrat (status ``ignored``),
        dans l'ordre du plan (flow map complet).

        La REPRISE d'un worker bloqué ne passe PAS par ici : ``run(...)`` en
        mode reprise appelle ``_run_worker(..., resume_request_id=...)``
        directement sur la tâche bloquée (rounds LLM minimaux).
        """
        skipped: Dict[str, Dict[str, Any]] = {}
        runnable: List[PlanTask] = []
        for task in tasks:
            decision = intent_decision_for(
                task.role, task.intent, active=self._intent_active,
            )
            if decision == "ignored":
                skipped[task.task_id] = self._skipped_worker(task)
                logger.info(
                    "agent_decision role=%s task=%s intent=%s confidence=%.2f "
                    "decision=ignored",
                    task.role, task.task_id, task.intent,
                    float(task.intent_confidence),
                )
                self._on_event(on_event, EV_WORKER_SKIPPED, {
                    "task_id": task.task_id, "role": task.role,
                    "intent": task.intent,
                    "intent_decision": "ignored",
                })
            else:
                runnable.append(task)

        specs = [(task, self._worker_prompt_for(task, prompt)) for task in runnable]

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

        # Ordre du PLAN conservé (workers filtrés inclus, traçabilité complète).
        for task in tasks:
            res = skipped.get(task.task_id)
            if res is None and results:
                res = results.pop(0)
            if res is None:  # pragma: no cover - défensif (désalignement)
                continue
            workers.append(res)
            if res["status"] != "ok":
                unexecuted.append(res)
        return workers, unexecuted

    def _skipped_worker(self, task: PlanTask) -> Dict[str, Any]:
        """Contrat d'un worker FILTRÉ par la politique d'intention (Approche B).

        Le worker n'est ni construit ni exécuté : le dict trace la décision
        (statut ``ignored``) pour le flow map — ce n'est NI un échec (aucun
        code d'erreur) NI une sous-tâche exécutée.
        """
        return {
            "task_id": task.task_id,
            "role": task.role,
            "status": "ignored",
            "intent": task.intent,
            "intent_decision": "ignored",
            "message": (
                f"Sous-tâche non exécutée : le rôle « {task.role} » est hors "
                f"périmètre de l'intention détectée (« {task.intent} »)."
            ),
            "duration_ms": 0.0,
        }

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

        agent = self._make_agent(
            task.role, max_rounds=effective_rounds,
            intent=task.intent, intent_confidence=task.intent_confidence,
        )
        # Compteur d'appels d'outils du worker (observabilité d'intention :
        # ``tools_called`` remonté dans le contrat du worker).
        tool_counter = {"count": 0}

        def tool_hook(payload):
            tool_counter["count"] += 1
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
            on_thinking=thinking_hook, tool_counter=tool_counter,
        )

    def _run_worker_core(
        self, agent, task, worker_prompt, tool_hook, forbidden_hook, started, on_event,
        resume_request_id: str | None = None,
        on_thinking=None,
        tool_counter: Optional[Dict[str, int]] = None,
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

        # Observabilité d'intention (SCRUM-101) : décision + outils du worker.
        tools_called = int((tool_counter or {}).get("count", 0))
        logger.info(
            "agent_decision role=%s task=%s intent=%s confidence=%.2f "
            "decision=executed tools_called=%d",
            task.role, task.task_id, task.intent,
            float(task.intent_confidence), tools_called,
        )

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
                "intent": task.intent,
                "intent_decision": "executed",
                "tools_called": tools_called,
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
            "intent": task.intent,
            "intent_decision": "executed",
            "tools_called": tools_called,
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
            "intent": getattr(task, "intent", "action"),
            "intent_decision": "executed",
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

    # --- Repli conversationnel (FSM : FALLBACK_CHAT, Approche B) ------------

    def _build_fallback_agent(self):
        """Agent de repli (aucun outil) : réponse conversationnelle directe.

        Distinct du lead de synthèse : le prompt système
        ``build_fallback_system`` couvre le cas « aucune sous-tâche exécutée »
        sans prétendre agréger des résultats d'agents. Instancié une seule
        fois (lazy, même client LLM dédié que le lead).
        """
        if self._fallback_subject is None:
            # AUCUN outil (tools={} explicite : sans cela AgentCore retombe
            # sur le registre global) — le repli ne doit RIEN exécuter.
            self._fallback_subject = AgentCore(
                self._lead_llm_client,
                system_prompt=build_fallback_system(),
                tools={},
                required_args={},
            )
        return self._fallback_subject

    def _fallback_answer(self, prompt: str) -> str:
        """Réponse conversationnelle directe du superviseur (sans outils)."""
        try:
            result = self._build_fallback_agent().run_detailed(str(prompt))
            return str(getattr(result, "answer", str(result)))
        except Exception as exc:
            logger.error("Repli conversationnel échoué : %s", exc)
            return (
                "Votre message semble conversationnel et aucune sous-tâche "
                "outillée n'était requise. Je n'ai cependant pas pu produire "
                f"de réponse : {exc}"
            )

    def _fallback_outcome(
        self,
        prompt: str,
        tasks: List[PlanTask],
        workers: List[Dict[str, Any]],
        intent_meta: Optional[Dict[str, Any]],
        started: float,
        fsm: MultiRunFSM,
        on_event,
        reason: str,
    ) -> Dict[str, Any]:
        """Contrat de sortie du repli conversationnel (FSM : FALLBACK_CHAT).

        Le ``fsm`` reçu est DÉJÀ dans ``fallback_chat`` (transition faite par
        l'appelant) : le superviseur produit la réponse finale directement
        (sans outils), puis synthesizing → completed. Le repli est MANDATOIRE
        quand tout le plan a été filtré : jamais de silence utilisateur.
        """
        self._on_event(on_event, EV_FALLBACK, {
            "reason": reason,
            "intent": (intent_meta or {}).get("intent"),
            "intent_confidence": (intent_meta or {}).get("confidence"),
        })
        final_answer = self._fallback_answer(prompt)
        fsm = fsm.transition(MultiRunState.SYNTHESIZING)
        fsm = fsm.transition(MultiRunState.COMPLETED)
        meta = intent_meta or {}
        outcome = {
            "status": "completed",
            "final_answer": final_answer,
            "fallback_chat": True,
            "plan": self._plan_dicts(tasks),
            "workers": workers,
            "unexecuted": [],
            "thinking": "",
            "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
            "fsm_state": fsm.state.value,
            "intent": meta.get("intent", "chat"),
            "intent_confidence": float(meta.get("confidence", 0.0)),
        }
        if self._budget_pool is not None:
            outcome["budget"] = self._budget_pool.snapshot()
        self._on_event(on_event, EV_DONE, {
            "status": outcome["status"],
            "answer": final_answer,
            "duration_ms": outcome["duration_ms"],
        })
        return outcome

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

            # 0. Intention GLOBALE (Approche B) : classifiée AVANT la
            # planification, pour que le plan lui-même s'adapte (chat ⇒
            # sous-tâches minimales ou vides ; action ⇒ plan outillé).
            intent_meta = self._classify_intent(str(prompt), on_event)

            # 1. Plan (planning → dispatch)
            fsm = MultiRunFSM.start()  # planning
            tasks, plan_error, raw_plan = self._plan(
                str(prompt), on_event, intent_meta=intent_meta,
            )
            if tasks is None:
                if (
                    (plan_error or {}).get("error_code") == PLAN_EMPTY
                    and (intent_meta or {}).get("intent") == "chat"
                ):
                    # Intention « chat » + plan vide ⇒ le planner a jugé
                    # qu'aucune action outillée n'était nécessaire : réponse
                    # conversationnelle directe (JAMAIS d'abort pour du chat).
                    fsm = fsm.transition(MultiRunState.FALLBACK_CHAT)
                    return self._fallback_outcome(
                        str(prompt), [], [], intent_meta, started, fsm,
                        on_event, reason="plan_empty_chat",
                    )
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
            # Stamp de l'intention GLOBALE sur chaque sous-tâche (Approche B) :
            # propagée aux workers (guidage prompt + contrat) puis filtrée par
            # rôle au dispatch.
            self._stamp_intent(tasks, intent_meta)
            fsm = fsm.transition(MultiRunState.DISPATCH)
            self._on_event(on_event, EV_PLAN, {"plan": self._plan_dicts(tasks)})

            # 1 bis. Pipeline des propositions de tools (SCRUM-99) : chaque
            # proposition est relue par le reviewer ; RIEN n'est enregistré
            # automatiquement (fail-closed, décision humaine via l'API).
            tool_proposals = self._handle_tool_proposals(on_event)

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

            # 2bis. Repli MANDATOIRE (Approche B) : toutes les sous-tâches ont
            # été filtrées par la politique d'intention ⇒ aucun worker n'a
            # tourné. Sans repli, la synthèse n'aurait RIEN à agréger (réponse
            # vide/silence) : le superviseur répond directement.
            if workers and all(
                w.get("intent_decision") == "ignored" for w in workers
            ):
                fsm = fsm.transition(MultiRunState.FALLBACK_CHAT)
                return self._fallback_outcome(
                    str(prompt), tasks, workers, intent_meta, started, fsm,
                    on_event, reason="all_workers_ignored",
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
                "plan": self._plan_dicts(tasks),
                "workers": workers,
                "unexecuted": unexecuted,
                "thinking": thinking,
                "duration_ms": round((time.perf_counter() - started) * 1000.0, 2),
                "fsm_state": fsm.state.value,
            }
            if tool_proposals:
                outcome["tool_proposals"] = tool_proposals
            if intent_meta is not None:
                outcome["intent"] = intent_meta.get("intent")
                outcome["intent_confidence"] = float(intent_meta.get("confidence", 0.0))
                outcome["fallback_chat"] = False
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
            "plan": self._plan_dicts(tasks),
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
            "plan": self._plan_dicts(tasks),
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
