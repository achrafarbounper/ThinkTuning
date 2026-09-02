"""Prompts de l'orchestration multi-agents (superviseur + synthèse).

Contient :
  - ``build_planner_prompt`` : prompt donné au LEAD pour décomposer la tâche
    en sous-tâches assignées aux rôles (sortie JSON stricte).
  - ``build_synthesis_prompt`` : prompt de synthèse — le superviseur agrège
    les résultats des workers SANS outils, en signalant explicitement les
    sous-tâches non exécutées (bloc « Ce qui n'a pas pu être exécuté »).
  - ``build_worker_task_prompt`` : bloc d'isolation stricte injecté sur le
    rôle d'un worker (contexte global résumé + sous-tâche, RIEN d'autre).
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .context import estimate_tokens
from .roles import get_role


def build_planner_prompt(prompt: str, role_names: List[str]) -> str:
    """Prompt du LEAD : planifier (décomposer) la demande en sous-tâches.

    Sortie attendue : UN SEUL JSON, soit une liste de tâches, soit un objet
    ``{"tasks": [...]}``. Chaque tâche : ``{"task_id", "role", "subtask"}``
    (+ ``dependencies`` optionnel). ``role`` DOIT appartenir à
    ``role_names``. Le format est ensuite validé par ``plan_validator``.
    """
    return (
        "Tu es le superviseur d'une équipe d'agents spécialisés. "
        "Décompose la demande de l'utilisateur en sous-tâches, chacune "
        f"assignée à UN rôle parmi : {', '.join(role_names)}.\n\n"
        "Demande :\n"
        f"{prompt}\n\n"
        "RÈGLES DU PLAN :\n"
        "- Chaque sous-tâche doit être autonome et réalisable par UN SEUL rôle.\n"
        "- Ne crée AUCUNE sous-tâche inutile : au minimum nécessaire, "
        "au maximum 5.\n"
        "- N'utilise que des rôles de la liste, jamais d'autres noms.\n"
        "- Une sous-tâche dont le rôle n'a pas d'outil adapté est à éviter.\n\n"
        'Réponds UNIQUEMENT avec UN SEUL JSON, en français. Soit une liste '
        'de tâches, soit :\n'
        '{"tasks": [\n'
        '  {"task_id": "task-1", "role": "<role>", "subtask": "Description précise"},\n'
        '  {"task_id": "task-2", "role": "<role>", "subtask": "...", '
        '"dependencies": ["task-1"]}\n'
        ']}\n'
        "Aucun texte autour du JSON."
    )


def build_worker_context(context_summary: str, subtask: str) -> str:
    """Bloc d'isolation stricte injecté dans un worker.

    Contexte global RÉSUMÉ + sous-tâche exacte. Aucun résultat d'un autre
    worker n'est injecté : chaque worker travaille isolément (V1).
    """
    return (
        "CONTEXTE GLOBAL (résumé de la demande de l'utilisateur) :\n"
        f"{context_summary}\n\n"
        "VOTRE SOUS-TÂCHE (vous ne devez traiter QUE celle-ci) :\n"
        f"{subtask}\n\n"
        "Réalisez cette sous-tâche uniquement avec les outils disponibles de "
        "votre rôle. Ne traitez pas d'autres aspects de la demande."
    )


def truncate_context(prompt: str, max_chars: int = 2400) -> str:
    """Résumé tronqué (déterministe, sans LLM) du contexte pour un worker.

    Politique d'isolation stricte : on borne la taille du contexte global
    injecté, pour maîtriser le budget en jetons de chaque worker.
    """
    text = (prompt or "").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…  [contexte global tronqué]"
def build_synthesis_prompt(
    original_prompt: str,
    workers: List[Dict[str, Any]],
    unexecuted: List[Dict[str, Any]],
) -> str:
    """Prompt du superviseur : produire la réponse finale cohérente.

    Le superviseur (lead) n'a AUCUN outil : il lit uniquement les résultats
    des workers et rédige la synthèse. Politique :
      - Ne PAS réécrire les résultats (fidélité) ;
      - Signaler explicitement chaque sous-tâche non exécutée (``unexecuted``) ;
      - Ne JAMAIS inventer le résultat d'un worker en échec.
    """
    workers_dump = json.dumps(workers, ensure_ascii=False, indent=2, default=str)
    unexecuted_dump = json.dumps(unexecuted, ensure_ascii=False, indent=2, default=str)

    body = (
        "Tu es le superviseur d'une équipe d'agents. Voici la demande "
        "originale, le plan exécuté et les résultats de chaque agent.\n\n"
        "DEMANDE ORIGINALE :\n"
        f"{original_prompt}\n\n"
        "RÉSULTATS DES WORKERS (JSON) :\n"
        f"{workers_dump}\n"
    )

    if unexecuted:
        body += (
            "\nSOUS-TÂCHES NON EXÉCUTÉES (JSON) :\n"
            f"{unexecuted_dump}\n"
            "\nIMPORTANT : plusieurs sous-tâches n'ont PAS pu être exécutées. "
            "Tu dois produire un paragraphe explicite commençant par "
            "« ⚠️ Ce qui n'a pas pu être exécuté : » qui liste ces tâches et "
            "leurs erreurs. Ne complète JAMAIS leur résultat : si une partie "
            "de la réponse manque, le dire honnêtement.\n"
        )

    body += (
        "\nRÉDUCTION DE LA RÉPONSE FINALE :\n"
        "- Rédige une réponse cohérente, en français, en texte normal "
        "(aucun JSON).\n"
        "- Appuie-toi UNIQUEMENT sur les résultats réellement produits par "
        "les workers ; ne réécris pas leur contenu, intègre-le.\n"
        "- Si un résultat est vide ou en erreur, signale-le sans inventer.\n"
        "- Structure ta réponse clairement (sections, listes si nécessaire)."
    )
    return body


def build_synthesis_system() -> str:
    """Prompt système du lead en phase de synthèse (aucun outil)."""
    return (
        "Tu es le superviseur coordonnateur d'équipe d'agents. "
        "Tu n'as AUCUN outil : tu ne fais que composer la réponse finale "
        "à partir des résultats des agents. Sois fidèle à ces résultats, "
        "signale explicitement ce qui n'a pas pu être fait, et ne réponds "
        "jamais de mémoire sur un fait que les agents n'ont pas obtenu."
    )


def worker_budget_exceeded(task_id: str, role: str) -> Dict[str, Any]:
    """Payload standard d'un worker qui a dépassé son budget de jetons."""
    return {
        "task_id": task_id,
        "role": role,
        "status": "error",
        "error_code": "TokenBudgetExceeded",
        "message": "Budget de jetons du worker dépassé.",
        "estimate_tokens": None,
    }