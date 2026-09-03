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
import re
import time
from collections.abc import Callable
from datetime import date
from typing import Any

from pydantic import BaseModel, Field

from app.agent.policies.budget import RunBudget
from app.agent.policies.sandbox_policy import decide_action
from app.domain.entities.plan import Action, Decision, Intent, Plan, PlanStep
from app.domain.entities.run import RunStatus  # noqa: F401  # ré-export domaine
from app.domain.errors import BudgetExceededError
from app.domain.ports import (
    EventBusPort,
    LLMClientPort,
    Message,
    ToolRegistryPort,
)

logger = logging.getLogger("thinktuning.agent.core")

# ============================================================
# MODÈLES DE RÉSULTAT
# ============================================================
# ``RunStatus`` est défini dans le domaine (app/domain/entities/run.py) et
# ré-exporté ici pour préserver ``from app.agent.core import RunStatus``.


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


# Marqueur de conclusion « FINAL » : les petits modèles le recrachent parfois
# NU au lieu du contenu de la réponse (fuite du protocole de format).
_FINAL_PREFIX_RE = re.compile(r"^FINAL\s*:\s*", re.IGNORECASE)
_FINAL_MARKER_RE = re.compile(r"^FINAL\s*:?\s*$", re.IGNORECASE)


def _sanitize_final_answer(response: str, traces: list[ActionTrace]) -> str:
    """Assainit une réponse finale « texte direct » du noyau.

    - retire un préfixe « FINAL: » éventuel ;
    - si le reste est vide ou réduit au marqueur nu (« FINAL »), remplace par
      le dernier résultat d'outil concluant, sinon par un message neutre
      (jamais le marqueur brut exposé à l'utilisateur).
    """
    text = _FINAL_PREFIX_RE.sub("", (response or "").strip()).strip()
    if not text or _FINAL_MARKER_RE.match(text):
        done = [t for t in reversed(traces)
                if t.status == "done" and t.result_summary]
        if done:
            logger.warning("final_answer_marker_leak -> fallback tool result")
            return done[0].result_summary
        logger.warning("final_answer_marker_leak -> neutral message")
        return ("Je n'ai pas pu produire de réponse à partir des informations "
                "collectées. Peux-tu reformuler ta demande ?")
    return text


# ============================================================
# SYSTEM PROMPT (généré depuis le registre réel des outils)
# ============================================================

_SYSTEM_PROMPT_HEADER = (
    "Tu es un agent outillé. Nous sommes le {today}. "
    "Tu peux utiliser les outils suivants :\n\n"
    "{tools}\n\n"
    """PROTOCOLE (strict) :
1. Si tu as besoin d'un outil, réponds UNIQUEMENT avec un JSON :
   {{"plan": [{{"tool": "<nom>", "args": {{...}}}}, ...]}}
2. Si tu n'as besoin d'aucun outil, réponds en texte normal (pas de JSON).
3. Après avoir reçu les résultats, conclus en t'appuyant UNIQUEMENT sur eux.
4. N'invente jamais d'outil ni d'argument : {tool_names} sont les seuls outils.
5. FIDÉLITÉ AUX OUTILS : tes connaissances internes ont une date de coupure
   et peuvent être OBSOLÈTES. Les résultats renvoyés par les outils sont la
   vérité terrain et PRIMENT sur ta mémoire, même quand ils la contredisent
   (surtout pour l'actualité, le sport, les faits récents). Ne doute JAMAIS
   d'un résultat d'outil et ne le qualifie jamais de « fictif » ou
   « spéculatif » au seul motif qu'il contredit ce que tu « sais ».
   Si une source te semble douteuse, rappelle un outil avec une requête plus
   ciblée au lieu de refuser de répondre.
"""
)


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
    return _SYSTEM_PROMPT_HEADER.format(
        tools="\n".join(descriptions), tool_names=names, today=date.today().isoformat()
    )


# ============================================================
# PARSING TOLÉRANT DU PLAN
# ============================================================

_PLAN_KEYS = ("plan", "tasks", "actions")

# Balises d'appel d'outil ajoutées par certains modèles autour du JSON
# (ex. « [TOOL_CALL] … [/TOOL_CALL] », « <tool_call> … </tool_call> ») :
# elles n'apportent rien, on les retire avant l'analyse.
_TOOL_CALL_TAGS = re.compile(r"</?\s*(?:\[?TOOL_CALL\]?|tool_call)\s*>", re.IGNORECASE)
# Séparateur de paire style Ruby / YAML (« tool => "web_search" »).
_HASH_ARROW = re.compile(r"=>")
# Clé « flag » CLI invoquée comme argument (« --query "..." »).
_FLAG_KEY = re.compile(r"--\s*([A-Za-z_][\w-]*)\s*")
# Clé non quotée devant « : » (ex. {tool: "web_search"}).
_UNQUOTED_KEY = re.compile(r"([{,]\s*)([A-Za-z_][\w-]*)\s*:")


def _repair_pseudo_json(candidate: str) -> str:
    """Répare les erreurs de syntaxe les plus fréquentes des LLM.

    Traitement (dans l'ordre) :
        - « => » devient « : » (notation hash Ruby) ;
        - clés non quotées devant « : » mises entre guillemets ;
        - arguments invoqués en style flag CLI (« --query "x" ») convertis
          en paires « "query": "x" ».
    """
    repaired = _HASH_ARROW.sub(":", candidate)
    repaired = _UNQUOTED_KEY.sub(lambda m: f'{m.group(1)}"{m.group(2)}":', repaired)
    repaired = _FLAG_KEY.sub(lambda m: f'"{m.group(1)}": ', repaired)
    return repaired


def _extract_plan_candidate(candidate: str) -> Plan | None:
    """Parse UN candidat : JSON strict puis version réparée."""
    import json

    for text in (candidate, _repair_pseudo_json(candidate)):
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, list):
            return _steps_from(parsed)
        if isinstance(parsed, dict):
            for key in _PLAN_KEYS:
                if isinstance(parsed.get(key), list):
                    return _steps_from(parsed[key])
            # Objet « action seule » : {"tool": ..., "args": {...}} — certains
            # modèles n'encapsulent pas dans un plan (malgré le protocole).
            if parsed.get("tool"):
                return _steps_from([parsed])
    return None


def extract_plan(raw: str) -> Plan | None:
    """Extrait un ``Plan`` d'une réponse LLM (tolérant prose/fences markdown).

    Formes acceptées : liste directe, {plan|tasks|actions: [...]}, objet
    « action seule » {"tool": ...}, JSON entouré de texte, balises
    [TOOL_CALL]/<tool_call>, pseudo-JSON réparé (« => », clés non quotées,
    flags « --arg »). Renvoie None si rien d'exploitable (=> réponse texte).
    """
    if not raw or not raw.strip():
        return None
    cleaned = _TOOL_CALL_TAGS.sub("", raw).strip()
    candidates: list[str] = [cleaned]
    # Fences markdown ```json ... ``` et blocs {…} isolés dans la prose.
    candidates.extend(m.strip() for m in re.findall(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL))
    candidates.extend(m.strip() for m in re.findall(r"\{.*\}", cleaned, re.DOTALL))
    for candidate in candidates:
        plan = _extract_plan_candidate(candidate)
        if plan is not None:
            return plan
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

# ============================================================
# GARDE-FOU « OUTIL ANNONCÉ MAIS JAMAIS APPELÉ »
# (porté de ia/agent/agent_core.py — bug « qui a gagné la coupe du monde
# 2026 » : un appel d'outil mal formé ou annoncé en prose ne doit JAMAIS être
# avalé comme une réponse texte légitime sans AU MOINS une relance.)
# ============================================================

_INTENT_MARKERS = (
    "appell",     # appelle / appeler
    "utiliser", "utilise",
    "lanc",       # lance / lançons
    "exécu", "execu",
    "vérifi",
    "recherch",
    "interrog",
    "je vais", "il faut", "dois ",
    "let me", "use ", "using", "call ", "calling",
    "search ", "fetch ", "check ", "query ",
    "tool_call",  # balise [TOOL_CALL] / <tool_call> mal formée
)
_ANNOUNCE_WINDOW = 80


def _detect_announced_tool(text: str, tool_names) -> str:
    """Nom du premier outil CONNU annoncé dans ``text``, sinon ``""``.

    Une annonce = nom d'outil du registre (frontières de mot) à moins de
    ``_ANNOUNCE_WINDOW`` caractères d'un marqueur d'intention d'appel.
    """
    if not text or not tool_names:
        return ""
    lowered = text.lower()
    pattern = re.compile(
        r"\b(?:" + "|".join(re.escape(name.lower()) for name in sorted(tool_names)) + r")\b"
    )
    for match in pattern.finditer(lowered):
        window = lowered[max(0, match.start() - _ANNOUNCE_WINDOW): match.end() + _ANNOUNCE_WINDOW]
        if any(marker in window for marker in _INTENT_MARKERS):
            return match.group(0)
    return ""


_NUDGE_MESSAGE = (
    "Ton message annonce utiliser l'outil « {tool} » mais AUCUN appel "
    "exploitable n'a été produit (JSON illisible ou absent) : aucune "
    "information réelle n'a donc été obtenue et ta réponse actuelle sort de "
    "mémoire. Jette-la. Renvoie UNIQUEMENT ce JSON strict, sans texte ni "
    "balise autour :\n"
    '{{"plan": [{{"tool": "{tool}", "args": {{...}}}}]}}'
)

# ============================================================
# GARDE-FOU « RÉSULTAT D'OUTIL REÇU MAIS CONTESTÉ »
# Certains modèles (notamment les gratuits) doutent du résultat d'un outil
# réussi quand il contredit leur mémoire (date de coupure obsolète) et
# REFUSENT de répondre (« pas encore eu lieu », « contenu fictif », …) au
# lieu de conclure sur le résultat obtenu. On relance UNE fois.
# ============================================================

_DISTRUST_MARKERS = (
    "ne peux pas confirmer",
    "ne peut pas confirmer",
    "pas encore eu lieu",
    "n'a pas encore eu lieu",
    "n'a pas eu lieu",
    "contenu fictif",
    "fictive",
    "non officiel",
    "spécul",
    "résultat affiché",
    "je ne peux pas te dire",
    "cannot confirm",
    "not yet taken place",
    "hasn't happened",
)

_TOOL_RESULT_NUDGE_MESSAGE = (
    "L'outil « {tool} » a bien été EXÉCUTÉ et a renvoyé un résultat RÉEL : "
    "il ne s'agit PAS d'un contenu fictif ou spéculatif. Tes connaissances "
    "internes ont une date de coupure et peuvent être obsolètes : le résultat "
    "de l'outil PRIME sur ta mémoire, même s'il la contredit. Conclus MAINTENANT "
    "en texte normal, en t'appuyant UNIQUEMENT sur le résultat obtenu ci-dessus."
)


def _detect_distrust(text: str) -> bool:
    """Vrai si la réponse finale suggère un refus de croire le résultat d'outil."""
    if not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _DISTRUST_MARKERS)


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
        on_tool_event: Callable[[dict], None] | None = None,
        enable_thinking: bool = False,
        on_thinking: Callable[[str], None] | None = None,
        event_bus: EventBusPort | None = None,
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._approval_gateway = approval_gateway
        self._max_tool_calls = max_tool_calls
        self._on_tool_event = on_tool_event
        # Bus d'événements (port pub/sub, optionnel) : le noyau publie les
        # événements de cycle de vie (run_start, tool_start/tool_end, thinking,
        # approval_pending, run_finished) sans dépendre d'un consommateur précis
        # (SSE, audit, métriques). Compatible avec les callbacks legacy :
        # ``on_tool_event`` / ``on_thinking`` restent diffusés en parallèle.
        self._event_bus = event_bus
        # Mode « Réflexion » : ``enable_thinking`` active le reconnement du
        # modèle pendant CHAQUE round ; ``on_thinking`` diffuse la trace en
        # temps réel (SSE thinking_delta), un tampon interne la conserve pour
        # le champ ``thinking`` du résultat final.
        self._enable_thinking = enable_thinking
        self._on_thinking = on_thinking
        # Le tampon est remis à zéro à chaque run (voir run()).
        self._thinking_parts: list[str] = []
        self._system_prompt = build_system_prompt(registry)

    def run(self, intent: Intent, history: list[Message] | None = None) -> AgentRunResult:
        """Exécute un run complet. Ne lève JAMAIS : le statut porte l'échec."""
        # Nouveau run (rebouffrage du même AgentCore en tests / atelier) :
        # le tampon de réflexion est propre à chaque exécution.
        self._thinking_parts = []
        if self._event_bus is not None:
            self._safe_emit("agent.run_start", prompt=intent.prompt)
        budget = RunBudget(max_llm_rounds=intent.max_rounds, max_tool_calls=self._max_tool_calls)
        traces: list[ActionTrace] = []
        rejected_prints: set[str] = set()
        # Une SEULE relance « outil annoncé mais jamais appelé » par run :
        # surcoût borné même face à un modèle têtu (convention anti-boucle).
        nudged = False
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
                if self._enable_thinking:
                    # Mode « Réflexion » : le client LLM diffuse son raisonnement
                    # via call_stream, on_thinking alimente la trace SSE temps
                    # réel et accumule dans le tampon du résultat final.
                    response = self._llm.call_stream(
                        messages,
                        on_thinking=self._capture_thinking,
                    )
                else:
                    response = self._llm.call(messages)
            except Exception as exc:
                logger.error("Échec du client LLM : %s", exc)
                return self._finalize(traces, budget, RunStatus.FAILED,
                                      answer=f"Erreur LLM : {exc}")
            plan = extract_plan(response)

            if plan is None:
                # Garde-fou : une réponse en texte qui ANNONCE un outil connu
                # (prose « je vais chercher… », balise [TOOL_CALL], pseudo-JSON
                # illisible) n'est PAS une conclusion légitime — on relance une
                # fois avec le format strict attendu, au lieu d'accepter une
                # réponse sortie de mémoire déguisée en recherche.
                announced = _detect_announced_tool(response, self._registry.tool_names())
                if announced and not nudged:
                    nudged = True
                    logger.warning(
                        "tool_intent_detected tool=%s", announced,
                    )
                    messages = [*messages,
                                {"role": "assistant", "content": response},
                                {"role": "user", "content": _NUDGE_MESSAGE.format(tool=announced)}]
                    continue
                # Garde-fou : un outil a RÉUSSI mais le modèle CONTESTE le
                # résultat (refus par confiance obsolète en sa mémoire) au lieu
                # de conclure dessus — relance unique orientée fidélité.
                done_tool = next(
                    (t.tool for t in traces if t.status == "done"), None
                )
                if done_tool and not nudged and _detect_distrust(response):
                    nudged = True
                    logger.warning("tool_result_distrusted tool=%s", done_tool)
                    messages = [*messages,
                                {"role": "assistant", "content": response},
                                {"role": "user",
                                 "content": _TOOL_RESULT_NUDGE_MESSAGE.format(tool=done_tool)}]
                    continue
                # Réponse texte directe (légitime)
                return self._finalize(
                    traces, budget, RunStatus.COMPLETED,
                    answer=_sanitize_final_answer(response, traces),
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

            # Émission temps réel (SSE) : annonce de l'appel d'outil.
            self._emit_tool_event({"event": "tool_start", "tool": action.tool,
                                   "args": action.args})
            started = time.perf_counter()
            try:
                value = func(**action.args)
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                results.append(f"[{step.task_id}] {action.tool} -> {self._summarize(value)}")
                traces.append(ActionTrace(
                    tool=action.tool, args=action.args, decision=decision.value,
                    status="done", result_summary=self._summarize(value),
                ))
                self._emit_tool_event({"event": "tool_result", "tool": action.tool,
                                       "status": "ok",
                                       "summary": self._summarize(value),
                                       "duration_ms": duration_ms})
            except Exception as exc:  # auto-correction : l'erreur retourne au LLM
                duration_ms = round((time.perf_counter() - started) * 1000, 2)
                results.append(f"[{step.task_id}] ERREUR d'exécution ({action.tool}) : {exc}")
                traces.append(ActionTrace(
                    tool=action.tool, args=action.args, decision=decision.value,
                    status="error", error=str(exc),
                ))
                self._emit_tool_event({"event": "tool_result", "tool": action.tool,
                                       "status": "error", "error": str(exc),
                                       "duration_ms": duration_ms})
        return "\n".join(results) if results else "Aucune action exécutée. Réponds à la question."

    def _capture_thinking(self, chunk: str) -> None:
        """Reçoit un fragment de raisonnement (mode « Réflexion »).

        Accumule dans le tampon du résultat final ET diffuse en temps réel via
        on_thinking (SSE thinking_delta). Ne lève jamais.
        """
        if not chunk:
            return
        self._thinking_parts.append(chunk)
        if self._on_thinking is not None:
            try:
                self._on_thinking(chunk)
            except Exception:  # pragma: no cover - défensif
                pass
        if self._event_bus is not None:
            self._safe_emit("agent.thinking", chunk=chunk)

    def _safe_emit(self, event_type: str, **data: Any) -> None:
        """Émet un événement sur le bus sans jamais faire échouer le run."""
        if self._event_bus is None:
            return
        try:
            self._event_bus.emit(event_type, **data)
        except Exception:  # pragma: no cover - défensif (le bus ne bloque pas)
            logger.exception("event_bus_emit_failed type=%s", event_type)

    def _emit_tool_event(self, event: dict) -> None:
        """Transmet un événement d'outil au callback SSE et/ou au bus.

        Le journal ne doit JAMAIS faire échouer le run : toute exception du
        callback ou de l'émission est avalée (même convention que
        run_store.append_tool_event).
        """
        if self._on_tool_event is not None:
            try:
                self._on_tool_event(event)
            except Exception:  # pragma: no cover - défensif
                pass
        if self._event_bus is not None:
            kind = event.get("event")
            if kind == "tool_start":
                self._safe_emit("agent.tool_start", tool=event.get("tool"),
                                args=event.get("args"))
            elif kind == "tool_result":
                self._safe_emit("agent.tool_end", tool=event.get("tool"),
                                status=event.get("status"),
                                summary=event.get("summary") or "",
                                error=event.get("error") or "",
                                duration_ms=event.get("duration_ms"))

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

    def _as_result(
        self,
        traces: list[ActionTrace],
        budget: RunBudget,
        status: RunStatus,
        answer: str = "",
        awaiting_action: Action | None = None,
    ) -> AgentRunResult:
        result = AgentRunResult(
            answer=answer,
            status=status,
            thinking="\n\n".join(self._thinking_parts).strip(),
            actions=list(traces),
            awaiting_action=awaiting_action,
            rounds_used=budget.snapshot().llm_rounds_used,
            tool_calls_used=budget.snapshot().tool_calls_used,
        )
        if self._event_bus is not None:
            # Soufflet EXACTEMENT une fois par run (tous les chemins passent par
            # _as_result, y compris _finalize) : observabilité / audit.
            self._safe_emit(
                "agent.run_finished",
                status=status.value,
                rounds_used=result.rounds_used,
                tool_calls_used=result.tool_calls_used,
            )
            if status is RunStatus.PENDING_APPROVAL and awaiting_action is not None:
                self._safe_emit(
                    "agent.approval_pending",
                    tool=awaiting_action.tool,
                    args=awaiting_action.args,
                )
        return result

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
