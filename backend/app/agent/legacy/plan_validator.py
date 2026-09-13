"""Validateur déterministe des plans produits par le superviseur (multi-agents).

Le superviseur (LLM) n'est PAS fiable pour produire des plans strictement
structurés : il oublie des rôles, forme mal les sous-tâches, duplique ou
cycle. Ce module impose des contraintes DÉTERMINISTES (aucune dépendance LLM)
et renvoie une structure stable consommable par l'orchestrateur.

Sortie ``ok=True``  -> tasks prêtes au dispatch, avec ``task_id``.
Sortie ``ok=False`` -> error_code + message (bucket "abort" côté orchestrateur).
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from .errors import (
    PLAN_CYCLE,
    PLAN_EMPTY,
    PLAN_VALIDATION_FAILED,
    TASK_DUPLICATE,
    TASK_INVALID,
    TASK_UNDEFINED,
)
from .json_parser import extract_json_blocks


class PlanTask:
    """Une sous-tâche validée, prête pour le dispatch.

    ``intent`` / ``intent_confidence`` : intention GLOBALE détectée par le
    superviseur (classifieur chat/action, Phase 4), stampée par l'orchestrateur
    APRÈS validation du plan puis appliquée au dispatch (Approche B : filtrage
    local par rôle, ``roles.INTENT_POLICY``). Défaut ``"action"`` : sans
    classification, le comportement historique est conservé (les workers
    outillés s'exécutent).
    """

    __slots__ = (
        "task_id", "role", "subtask", "dependencies",
        "intent", "intent_confidence",
    )

    def __init__(
        self,
        task_id: str,
        role: str,
        subtask: str,
        dependencies: List[str],
        intent: str = "action",
        intent_confidence: float = 0.0,
    ):
        self.task_id = task_id
        self.role = role
        self.subtask = subtask
        self.dependencies = dependencies
        self.intent = intent
        self.intent_confidence = intent_confidence


class ValidationResult:
    """Résultat de la validation : succès (tasks) ou échec (error_code)."""

    __slots__ = (
        "ok", "tasks", "error_code", "message", "valid_roles",
        "tool_proposals", "tool_proposal_notes",
    )

    def __init__(
        self,
        ok: bool,
        tasks: Optional[List[PlanTask]] = None,
        error_code: str = "",
        message: str = "",
        valid_roles: Optional[List[str]] = None,
        tool_proposals: Optional[List[Dict[str, Any]]] = None,
        tool_proposal_notes: Optional[List[Dict[str, Any]]] = None,
    ):
        self.ok = ok
        self.tasks = tasks or []
        self.error_code = error_code
        self.message = message
        self.valid_roles = valid_roles or []
        # SCRUM-99 : propositions de tools embarquées dans le plan
        # (pseudo-rôle ``propose_tool``), séparées des tâches exécutables.
        self.tool_proposals = tool_proposals or []
        self.tool_proposal_notes = tool_proposal_notes or []

    def to_dict(self) -> Dict[str, Any]:
        if self.ok:
            payload = {
                "ok": True,
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "role": t.role,
                        "subtask": t.subtask,
                        "dependencies": t.dependencies,
                        "intent": t.intent,
                        "intent_confidence": t.intent_confidence,
                    }
                    for t in self.tasks
                ],
            }
        else:
            payload = {
                "ok": False,
                "error_code": self.error_code,
                "message": self.message,
                "valid_roles": self.valid_roles,
            }
        # SCRUM-99 : propositions de tools incluses seulement si présentes,
        # pour ne pas modifier la forme des payloads historiques (tests, API).
        if self.tool_proposals:
            payload["tool_proposals"] = list(self.tool_proposals)
        if self.tool_proposal_notes:
            payload["tool_proposal_notes"] = list(self.tool_proposal_notes)
        return payload


def _extract_plan(raw: str) -> Optional[List[Dict[str, Any]]]:
    """Extrait la liste de tâches du plan brut (JSON tolérant).

    Deux formes sont acceptées :
      - une LISTE directe de tâches ``[{role, subtask}, …]`` ;
      - un objet ``{"tasks": [{role, subtask}, …]}``.

    On tente d'abord un parse JSON complet (couvre ``[]``, une liste directe
    ou un objet ``{tasks:…}``), puis on retombe sur ``extract_json_blocks``
    pour les réponses entourées de prose / fences markdown. Dans ce second cas,
    ``extract_json_blocks`` découpe chaque ``{…}`` individuellement : on les
    rassemble en la liste de tâches.
    """
    if not raw or not raw.strip():
        return None

    # 1) Parse JSON complet (gère '[]', liste directe, {tasks: [...]}).
    try:
        candidate = json.loads(raw)
    except (ValueError, TypeError):
        candidate = None
    if isinstance(candidate, list):
        return candidate
    if isinstance(candidate, dict) and isinstance(candidate.get("tasks"), list):
        return candidate["tasks"]

    # 2) Extraction souple (prose / fences) via extract_json_blocks.
    blocks = extract_json_blocks(raw)
    if not blocks:
        return None
    first = blocks[0]
    if isinstance(first, list):
        return first
    if isinstance(first, dict):
        tasks = first.get("tasks")
        if isinstance(tasks, list):
            return tasks
        # Liste directe découpée en task-dicts individuels : les rassembler.
        if all(isinstance(b, dict) and "role" in b for b in blocks):
            return blocks
    return None


def _topo_order_ok(tasks: List[PlanTask]) -> bool:
    """Vrai si le graphe de dépendances est acyclique (tri topologique)."""
    by_id = {t.task_id: t for t in tasks}
    visited: Dict[str, int] = {}  # 0 = en cours, 1 = terminé

    def dfs(task_id: str) -> bool:
        state = visited.get(task_id)
        if state == 1:
            return True
        if state == 0:
            return False  # cycle détecté
        visited[task_id] = 0
        for dep in by_id[task_id].dependencies:
            if dep in by_id and not dfs(dep):
                return False
        visited[task_id] = 1
        return True

    return all(dfs(t.task_id) for t in tasks)


# SCRUM-99 : pseudo-rôle du plan qui porte une PROPOSITION de tool (jamais
# dispatché comme worker). Même règle de nom que le standard
# ``thinktuning.tool/v1`` (cf. ``ia/tools/tool_schema.py``).
TOOL_PROPOSAL_ROLE = "propose_tool"
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")


def _extract_tool_proposal(
    item: Dict[str, Any], fallback_task_id: str,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Normalise une tâche ``propose_tool`` -> (proposition | None, raison).

    Validation minimale DÉTERMINISTE (la validation complète du standard
    ``thinktuning.tool/v1`` a lieu à l'enregistrement, dans la ToolRegistry) :
    nom ^[a-z][a-z0-9_]{1,63}$, description non vide, parameters objet.
    """
    tool = item.get("tool")
    if not isinstance(tool, dict):
        return None, "proposition sans objet « tool » (attendu : tool: {...})"
    name = tool.get("name")
    if not isinstance(name, str) or not _TOOL_NAME_RE.match(name):
        return None, f"nom de tool invalide : « {name} »"
    description = tool.get("description")
    if not isinstance(description, str) or not description.strip():
        return None, f"« {name} » : description absente ou vide"
    parameters = tool.get("parameters") or {}
    if not isinstance(parameters, dict):
        return None, f"« {name} » : « parameters » doit être un objet"
    required_args = tool.get("required_args")
    if not isinstance(required_args, list):
        required_args = []
    proposal: Dict[str, Any] = {
        "name": name,
        "description": description.strip(),
        "category": (
            tool.get("category") if isinstance(tool.get("category"), str)
            and tool.get("category") else "custom"
        ),
        "version": str(tool.get("version") or "1.0"),
        "required_args": [a for a in required_args if isinstance(a, str)],
        "parameters": parameters,
        "task_id": fallback_task_id,
        "subtask": (item.get("subtask") or "").strip(),
    }
    if isinstance(tool.get("safety"), dict):
        proposal["safety"] = tool["safety"]
    return proposal, None


def validate_plan(
    raw: str,
    roles: List[str],
    max_roles: int = 5,
    preprocess: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
    max_tool_proposals: int = 1,
) -> ValidationResult:
    """Valide un plan brut contre les rôles connus et les contraintes d'intégrité.

    ``preprocess`` : hook optionnel appliqué aux tâches brutes avant la
    validation structurelle — utilisé par le planner hybride
    (``plan_correct.correct_plan``) pour appliquer les règles métier
    déterministes (diagnostics → ops, shell whitelisté, SQL mutant rejeté…).
    La validation ici reste la DERNIÈRE barrière.

    Contraintes imposées (déterministes) :
      - format JSON strict (liste de tâches, ou {tasks:[...]}) ;
      - rôle existant dans ``roles`` ;
      - sous-tâche non vide ;
      - pas de duplication (task_id / (role, subtask)) ;
      - pas de circularité si ``dependencies`` est fourni ;
      - nombre de tâches borné par ``max_roles``.

    SCRUM-99 : les tâches de pseudo-rôle ``propose_tool`` (portant un objet
    ``tool``) ne sont PAS dispatchées : elles sont extraites, validées
    minimalement, bornées par ``max_tool_proposals`` et renvoyées dans
    ``ValidationResult.tool_proposals`` (rejets tracés dans
    ``tool_proposal_notes``) pour le pipeline de review/enregistrement.
    """
    valid_roles = list(roles)

    tasks_raw = _extract_plan(raw)
    if tasks_raw is None:
        return ValidationResult(
            ok=False,
            error_code=PLAN_VALIDATION_FAILED,
            message="Le plan n'est pas un JSON exploitable (liste de tâches attendue).",
            valid_roles=valid_roles,
        )

    if not tasks_raw:
        return ValidationResult(
            ok=False,
            error_code=PLAN_EMPTY,
            message="Le plan est vide : aucune sous-tâche à exécuter.",
            valid_roles=valid_roles,
        )

    # Passe de correction DÉTERMINISTE (planner hybride) : le superviseur (LLM)
    # propose un plan, ``preprocess`` applique les règles métier (diagnostics →
    # ops, shell whitelisté, SQL mutant rejeté…). Peut lever ``PlanRejected`` —
    # l'orchestrateur traduit en abort global (même bucket qu'un plan invalide).
    if preprocess is not None:
        tasks_raw = list(preprocess(tasks_raw))

    if len(tasks_raw) > max_roles:
        return ValidationResult(
            ok=False,
            error_code=PLAN_VALIDATION_FAILED,
            message=f"Le plan dépasse la borne de {max_roles} sous-tâches.",
            valid_roles=valid_roles,
        )

    tasks: List[PlanTask] = []
    seen_ids = set()
    seen_pairs = set()
    errors: List[str] = []
    # SCRUM-99 : collecte des propositions de tools du plan.
    tool_proposals: List[Dict[str, Any]] = []
    tool_notes: List[Dict[str, Any]] = []

    for index, item in enumerate(tasks_raw, start=1):
        task_id = item.get("task_id") if isinstance(item, dict) else None
        role = item.get("role") if isinstance(item, dict) else None
        subtask = item.get("subtask") if isinstance(item, dict) else None
        dependencies = (
            item.get("dependencies") if isinstance(item, dict) else None
        )

        # SCRUM-99 : pseudo-rôle « propose_tool » — extraire, ne pas dispatch.
        if role == TOOL_PROPOSAL_ROLE:
            proposal_id = task_id if isinstance(task_id, str) and task_id else f"task-{index}"
            proposal, reason = _extract_tool_proposal(
                item if isinstance(item, dict) else {}, proposal_id,
            )
            if proposal is None:
                tool_notes.append({"task_id": proposal_id, "reason": reason})
                continue
            if any(p["name"] == proposal["name"] for p in tool_proposals):
                tool_notes.append({
                    "task_id": proposal_id, "name": proposal["name"],
                    "reason": f"tool « {proposal['name']} » déjà proposé dans ce plan",
                })
                continue
            if len(tool_proposals) >= max_tool_proposals:
                tool_notes.append({
                    "task_id": proposal_id, "name": proposal["name"],
                    "reason": (
                        f"plafond de {max_tool_proposals} proposition(s) "
                        "de tool par plan atteint"
                    ),
                })
                continue
            tool_proposals.append(proposal)
            continue

        if not isinstance(role, str) or role not in roles:
            errors.append(
                f"tâche #{index} : rôle « {role} » inconnu (valides : "
                f"{', '.join(valid_roles)})"
            )
            continue
        if not isinstance(subtask, str) or not subtask.strip():
            errors.append(f"tâche #{index} : sous-tâche vide ou non textuelle")
            continue
        if not isinstance(task_id, str) or not task_id.strip():
            task_id = f"task-{index}"
        if task_id in seen_ids:
            errors.append(f"tâche #{index} : task_id dupliqué « {task_id} »")
            continue
        pair = (role, subtask.strip())
        if pair in seen_pairs:
            errors.append(
                f"tâche #{index} : sous-tâche dupliquée pour le rôle « {role} »"
            )
            continue
        if not isinstance(dependencies, list):
            dependencies = []
        seen_ids.add(task_id)
        seen_pairs.add(pair)
        tasks.append(
            PlanTask(
                task_id=task_id,
                role=role,
                subtask=subtask.strip(),
                dependencies=[d for d in dependencies if isinstance(d, str)],
            )
        )

    if errors:
        return ValidationResult(
            ok=False,
            error_code=TASK_INVALID,
            message=" ; ".join(errors),
            valid_roles=valid_roles,
        )

    if not tasks and not tool_proposals:
        return ValidationResult(
            ok=False,
            error_code=TASK_UNDEFINED,
            message="Aucune sous-tâche valide dans le plan.",
            valid_roles=valid_roles,
        )

    if not _topo_order_ok(tasks):
        return ValidationResult(
            ok=False,
            error_code=PLAN_CYCLE,
            message="Le plan contient une circularité de dépendances.",
            valid_roles=valid_roles,
        )

    return ValidationResult(
        ok=True,
        tasks=tasks,
        tool_proposals=tool_proposals,
        tool_proposal_notes=tool_notes,
    )
