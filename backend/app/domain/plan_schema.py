"""Validation JSON déterministe des plans d'agent (P2 lot 17) — DOMAINE pur.

Généralisation du ``plan_validator`` historique (``ia/agent/plan_validator.py``,
spécifique à l'orchestrateur multi-agents : rôles, dépendances, topologie).
Ce module traite le CAS GÉNÉRAL du plan « actions outillées » produit par un
LLM dans la boucle agentique (``{"plan": [{"tool": ..., "args": {...}}, ...]}``)
avec des contraintes structurelles ET sémantiques :

    - au noyau v2  (``app/agent/core.py``) — plan plat d'actions ;
    - à la red-team (script trimestriel) — malformed JSON, outils inconnus,
      arguments dépassés, injection par les args ;
    - aux transports MCP si un jour ils acceptent des plans libres.

Ce qui est validé (échec => erreur déterministe, code stable) :
    1. parse JSON tolérant — fences markdown, texte périphérique ;
    2. forme : objet ``{plan|tasks|actions: [...]}`` OU liste directe
       (même souplesse que le parsing historique du noyau) ;
    3. chaque étape : ``tool`` non vide, ``args`` dictionnaire JSON-safe,
       taille bornée (nb d'actions, nb d'args, longueur des chaînes) ;
    4. si un registre d'outils est fourni : outil CONNU exigé (deny-by-default
       pour les outils inconnus — l'auto-correction du noyau rejoue proprement) ;
    5. interdits : clés inconnues à l'étape (fail-fast).

Aucune I/O, aucun appel LLM — consommable dans les tests triviaux et par
les scripts de red-team. Codes d'erreur alignés sur ``PlanErrorCode``
(``app/domain/entities/plan.py``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from app.domain.entities.plan import PlanErrorCode

# Limites structurelles (coûts bornés : un plan LLM ne doit pas être géant).
MAX_PLAN_ACTIONS = 20
MAX_ARGS_PER_ACTION = 16
MAX_ARG_STRING_CHARS = 4000
MAX_PLAN_RAW_CHARS = 64_000

# Clés acceptées dans un objet plan (enveloppe) — mêmes aliases que le
# parsing historique du noyau.
_WRAPPER_KEYS = ("plan", "tasks", "actions")

# Clés autorisées par étape (fail-fast : une clé inconnue = plan corrompu).
_STEP_KEYS = ("tool", "args", "task_id", "subtask")

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


@dataclass(frozen=True)
class PlanStepValidation:
    """Une étape validée (contrat stable pour l'exécuteur)."""

    tool: str
    args: dict[str, Any]
    task_id: str = ""


@dataclass
class AgentPlanValidation:
    """Résultat de la validation d'un plan LLM (ok ou erreur typée)."""

    ok: bool
    steps: list[PlanStepValidation] = field(default_factory=list)
    error_code: PlanErrorCode | None = None
    message: str = ""

    def to_dict(self) -> dict:
        if self.ok:
            return {
                "ok": True,
                "steps": [
                    {"tool": s.tool, "args": s.args, "task_id": s.task_id}
                    for s in self.steps
                ],
            }
        return {
            "ok": False,
            "error_code": self.error_code.value if self.error_code else "",
            "message": self.message,
        }


def _extract_json_candidates(raw: str) -> list[str]:
    """Découpe la réponse LLM en candidats JSON (fences + blocs ``{…}``)."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return []
    candidates: list[str] = [cleaned]
    candidates.extend(m.strip() for m in _FENCE_RE.findall(cleaned))
    candidates.extend(m.strip() for m in re.findall(r"\{.*\}", cleaned, re.DOTALL))
    # Liste directe ``[...]`` au milieu de prose.
    candidates.extend(
        m.strip() for m in re.findall(r"\[.*\]", cleaned, re.DOTALL) if "{" in m
    )
    return sorted(set(candidates), key=len)


def _parse_json_lenient(candidate: str) -> Any:
    """JSON strict, puis version réparée (clés non quotées, ``=>``, virgules)."""
    try:
        return json.loads(candidate)
    except ValueError:
        pass
    repaired = re.sub(r"([{,\s])([A-Za-z_][\w-]*)\s*:", r'\1"\2":', candidate)
    repaired = re.sub(r"=>", ":", repaired)
    repaired = re.sub(r",\s*(?=[}\]])", "", repaired)
    try:
        return json.loads(repaired)
    except ValueError:
        return None


def _step_items(parsed: Any) -> list[Any] | None:
    """Normalise un objet/ligne en liste d'étapes candidates."""
    if isinstance(parsed, list):
        return parsed
    if isinstance(parsed, dict):
        for key in _WRAPPER_KEYS:
            if isinstance(parsed.get(key), list):
                return parsed[key]
        if isinstance(parsed.get("tool"), str):  # action seule
            return [parsed]
    return None


def validate_agent_plan(
    raw: str | dict,
    *,
    known_tools: set[str] | None = None,
    max_actions: int = MAX_PLAN_ACTIONS,
) -> AgentPlanValidation:
    """Valide un plan LLM (chaîne ou dict déjà parsé) contre les contraintes.

    Args:
        raw: réponse brute du LLM (chaîne) OU objet déjà parsé (dict/list).
        known_tools: registre d'outils autorisés ; ``None`` = pas de filtre
                     outil (validation structurelle uniquement).
        max_actions: plafond d'actions (défaut 20 — anti-abus).

    Retourne toujours un ``AgentPlanValidation`` (jamais d'exception) — le
    code d'erreur est porté par le champ ``error_code``.
    """
    if isinstance(raw, (dict, list)):
        parsed: Any = raw
    else:
        if raw is None or not str(raw).strip():
            return AgentPlanValidation(
                ok=False, error_code=PlanErrorCode.PLAN_EMPTY, message="Plan vide."
            )
        if len(str(raw)) > MAX_PLAN_RAW_CHARS:
            return AgentPlanValidation(
                ok=False,
                error_code=PlanErrorCode.PLAN_VALIDATION_FAILED,
                message=f"Plan trop volumineux (> {MAX_PLAN_RAW_CHARS} caractères).",
            )
        parsed = None
        for candidate in _extract_json_candidates(str(raw)):
            parsed = _parse_json_lenient(candidate)
            if _step_items(parsed) is not None:
                break

    steps_candidates = _step_items(parsed) if parsed is not None else None
    if steps_candidates is None:
        return AgentPlanValidation(
            ok=False,
            error_code=PlanErrorCode.PLAN_VALIDATION_FAILED,
            message='Aucun plan JSON exploitable (attendu : {"plan": [...]} ou une liste).',
        )

    if not steps_candidates:
        return AgentPlanValidation(
            ok=False, error_code=PlanErrorCode.PLAN_EMPTY, message="Plan sans action."
        )

    if len(steps_candidates) > max_actions:
        return AgentPlanValidation(
            ok=False,
            error_code=PlanErrorCode.PLAN_VALIDATION_FAILED,
            message=f"Plan trop long ({len(steps_candidates)} actions > {max_actions}).",
        )

    # --- Validation de chaque étape -----------------------------------------
    errors: list[str] = []
    steps: list[PlanStepValidation] = []
    for index, item in enumerate(steps_candidates):
        if not isinstance(item, dict):
            errors.append(f"action #{index} : attendu un objet, reçu {type(item).__name__}")
            continue
        unknown_keys = set(item) - set(_STEP_KEYS)
        if unknown_keys:
            errors.append(
                f"action #{index} : clés inconnues {sorted(unknown_keys)} "
                "(tool/args uniquement)"
            )
            continue
        tool = item.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            errors.append(f"action #{index} : 'tool' absent ou non textuel")
            continue
        tool = tool.strip()
        if known_tools is not None and tool not in known_tools:
            errors.append(
                f"action #{index} : outil '{tool}' inconnu "
                f"(valides : {', '.join(sorted(known_tools))})"
            )
            continue
        args = item.get("args", {})
        if not isinstance(args, dict):
            errors.append(f"action #{index} : 'args' doit être un objet JSON")
            continue
        if len(args) > MAX_ARGS_PER_ACTION:
            errors.append(
                f"action #{index} : trop d'arguments ({len(args)} > "
                f"{MAX_ARGS_PER_ACTION})"
            )
            continue
        oversized = [
            str(k)
            for k, v in args.items()
            if isinstance(v, str) and len(v) > MAX_ARG_STRING_CHARS
        ]
        if oversized:
            errors.append(
                f"action #{index} : arguments trop longs {oversized[:3]} "
                f"(> {MAX_ARG_STRING_CHARS} caractères)"
            )
            continue
        try:
            json.dumps(args, default=str)
        except (TypeError, ValueError) as exc:
            errors.append(f"action #{index} : args non sérialisables JSON ({exc})")
            continue
        steps.append(
            PlanStepValidation(
                tool=tool,
                args=args,
                task_id=str(item.get("task_id") or ""),
            )
        )

    if errors:
        return AgentPlanValidation(
            ok=False,
            error_code=PlanErrorCode.TASK_INVALID,
            message=" ; ".join(errors),
        )

    return AgentPlanValidation(ok=True, steps=steps)


__all__ = [
    "AgentPlanValidation",
    "MAX_PLAN_ACTIONS",
    "PlanStepValidation",
    "validate_agent_plan",
]
