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
from typing import Any, Callable, Dict, List, Optional

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
    """Une sous-tâche validée, prête pour le dispatch."""

    __slots__ = ("task_id", "role", "subtask", "dependencies")

    def __init__(self, task_id: str, role: str, subtask: str, dependencies: List[str]):
        self.task_id = task_id
        self.role = role
        self.subtask = subtask
        self.dependencies = dependencies


class ValidationResult:
    """Résultat de la validation : succès (tasks) ou échec (error_code)."""

    __slots__ = ("ok", "tasks", "error_code", "message", "valid_roles")

    def __init__(
        self,
        ok: bool,
        tasks: Optional[List[PlanTask]] = None,
        error_code: str = "",
        message: str = "",
        valid_roles: Optional[List[str]] = None,
    ):
        self.ok = ok
        self.tasks = tasks or []
        self.error_code = error_code
        self.message = message
        self.valid_roles = valid_roles or []

    def to_dict(self) -> Dict[str, Any]:
        if self.ok:
            return {
                "ok": True,
                "tasks": [
                    {
                        "task_id": t.task_id,
                        "role": t.role,
                        "subtask": t.subtask,
                        "dependencies": t.dependencies,
                    }
                    for t in self.tasks
                ],
            }
        return {
            "ok": False,
            "error_code": self.error_code,
            "message": self.message,
            "valid_roles": self.valid_roles,
        }


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


def validate_plan(
    raw: str,
    roles: List[str],
    max_roles: int = 5,
    preprocess: Optional[Callable[[List[Dict[str, Any]]], List[Dict[str, Any]]]] = None,
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

    for index, item in enumerate(tasks_raw, start=1):
        task_id = item.get("task_id") if isinstance(item, dict) else None
        role = item.get("role") if isinstance(item, dict) else None
        subtask = item.get("subtask") if isinstance(item, dict) else None
        dependencies = (
            item.get("dependencies") if isinstance(item, dict) else None
        )

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

    if not tasks:
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

    return ValidationResult(ok=True, tasks=tasks)
