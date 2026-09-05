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


def build_planner_prompt(
    prompt: str,
    role_names: List[str],
    role_tools: Optional[Dict[str, List[str]]] = None,
    intent: Optional[str] = None,
    intent_confidence: Optional[float] = None,
    allow_tool_proposals: bool = False,
    max_tool_proposals: int = 1,
) -> str:
    """Prompt du LEAD : planifier (décomposer) la demande en sous-tâches.

    Sortie attendue : UN SEUL JSON, soit une liste de tâches, soit un objet
    ``{"tasks": [...]}``. Chaque tâche : ``{"task_id", "role", "subtask"}``
    (+ ``dependencies`` optionnel). ``role`` DOIT appartenir à
    ``role_names``. Le format est ensuite validé par ``plan_validator``.

    ``role_tools`` (optionnel) : ``{rôle: [outils réels]}`` — injecte les
    CAPACITÉS de chaque rôle pour que le superviseur ne choisisse pas un rôle
    inadapté (diagnostics → ops, jamais shell pour du CPU simple).

    ``intent`` / ``intent_confidence`` (optionnels, Approche B) : intention
    GLOBALE détectée AVANT la planification — le plan lui-même s'adapte :
    intention « chat » ⇒ sous-tâches uniquement si une action réelle est
    clairement nécessaire (sinon liste vide, le repli conversationnel répond) ;
    intention « action » ⇒ plan outillé normal.

    ``allow_tool_proposals`` (SCRUM-99) : autorise le pseudo-rôle
    ``propose_tool`` — le planner peut proposer au plus
    ``max_tool_proposals`` NOUVEAU(x) outil(s) (objet ``tool`` embarqué).
    La proposition est relue par un humain ; l'outil n'est PAS disponible
    dans l'exécution en cours.
    """
    capabilities = ""
    if role_tools:
        lines = [
            f"- {role} : {', '.join(role_tools[role])}"
            for role in role_names
            if role in role_tools
        ]
        capabilities = (
            "\nCAPACITÉS RÉELLES DES RÔLES (outils disponibles) :\n"
            + "\n".join(lines)
            + "\n"
        )

    intent_block = ""
    if intent == "chat":
        intent_block = (
            "\nINTENTION DÉTECTÉE (classifieur) : « chat » — la demande semble "
            "CONVERSATIONNELLE (salutation, question générale, opinion). Ne "
            "crée des sous-tâches QUE si une action réelle avec un outil est "
            "clairement nécessaire ; sinon renvoie une liste vide [].\n"
        )
    elif intent == "action":
        intent_block = (
            "\nINTENTION DÉTECTÉE (classifieur) : « action » — la demande "
            "exige une exécution réelle : planifie les sous-tâches outillées "
            "nécessaires.\n"
        )

    # SCRUM-99 : règles de proposition de tools (pseudo-rôle propose_tool).
    if allow_tool_proposals:
        proposal_block = (
            "\nOUTILS PERSONNALISÉS (fonctionnalité activée) :\n"
            "Si et seulement si la demande exige une capacité qu'AUCUN outil "
            "existant ne couvre, vous pouvez PROPOSER au plus "
            f"{max_tool_proposals} NOUVEAU(x) outil(s) via le pseudo-rôle "
            "« propose_tool » (SEULE exception autorisée à la liste des "
            "rôles). Format d'une proposition :\n"
            '{"task_id": "propose-1", "role": "propose_tool", '
            '"subtask": "Pourquoi cet outil est nécessaire", "tool": '
            '{"name": "get_weather", "description": "...", "category": "api", '
            '"required_args": ["city"], "parameters": {"city": {"type": '
            '"string", "required": true, "description": "..."}}}}\n'
            "RÈGLES DES PROPOSITIONS :\n"
            "- « name » : minuscules, chiffres et underscores uniquement "
            "(2 à 64 caractères) ; JAMAIS le nom d'un outil existant.\n"
            "- L'outil proposé n'est PAS disponible dans cette exécution : "
            "NE créez AUCUNE sous-tâche qui l'appelle ; planifiez le reste "
            "avec les outils existants.\n"
            "- Toute proposition est relue par un humain AVANT "
            "enregistrement : ne proposez jamais d'outil destructeur, "
            "d'accès non filtré ou d'exécution de code arbitraire.\n"
            "- Zéro proposition si les outils existants suffisent.\n"
        )
        roles_rule = (
            "- N'utilise que des rôles de la liste ; SEULE exception : le "
            "pseudo-rôle « propose_tool » (voir OUTILS PERSONNALISÉS).\n"
        )
    else:
        proposal_block = ""
        roles_rule = "- N'utilise que des rôles de la liste, jamais d'autres noms.\n"

    return (
        "Tu es le superviseur d'une équipe d'agents spécialisés. "
        "Décompose la demande de l'utilisateur en sous-tâches, chacune "
        f"assignée à UN rôle parmi : {', '.join(role_names)}.\n\n"
        "Demande :\n"
        f"{prompt}\n\n"
        f"{capabilities}"
        f"{intent_block}"
        f"{proposal_block}\n"
        "RÈGLES DU PLAN :\n"
        "- Chaque sous-tâche doit être autonome et réalisable par UN SEUL rôle.\n"
        "- Ne crée AUCUNE sous-tâche inutile : au minimum nécessaire, "
        "au maximum 5.\n"
        f"{roles_rule}\n"
        "- Une sous-tâche dont le rôle n'a pas d'outil adapté est à éviter.\n"
        "- DIAGNOSTIC / information système (OS, CPU, RAM, disque, GPU, "
        "versions python, variables d'env) : rôle « ops » (env_info, "
        "disk_usage, gpu_info). JAMAIS « shell » pour cela.\n"
        "- LECTURE SEULE (consulter, lister, chercher, interroger) : "
        "aucun risque → le worker n'a pas besoin de validation humaine.\n"
        "- « shell » est RÉSERVÉ aux commandes d'exécution simple ; une "
        "commande complexe ou non sûre est à éviter.\n"
        "- DONNÉES (SQL) : rôle « data » ; uniquement SELECT/WITH/EXPLAIN/PRAGMA."
        " Toute mutation SQL (INSERT/UPDATE/DELETE/DROP…) est INTERDITE.\n"
        "- RECHERCHE / LECTURE WEB : rôle « web ».\n\n"
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


def build_fallback_system() -> str:
    """Prompt système du repli conversationnel (intention « chat »).

    Approche B : utilisé quand AUCUNE sous-tâche outillée n'est exécutée
    (filtrage par intention) ou que le planner a renvoyé un plan vide sur
    une intention « chat ». Le superviseur répond DIRECTEMENT, sans outils,
    sans prétendre avoir exécuté quoi que ce soit.
    """
    return (
        "Tu es l'assistant de la plateforme ThinkTuning. La demande de "
        "l'utilisateur a été détectée comme CONVERSATIONNELLE : aucune "
        "sous-tâche outillée n'était nécessaire et AUCUNE n'a été exécutée. "
        "Réponds directement à la demande, en français, en texte normal "
        "(aucun JSON), de façon claire et utile. Si la demande nécessitait "
        "en réalité une action (recherche web, calcul, manipulation de "
        "fichiers…), dis-le honnêtement et invite l'utilisateur à reformuler "
        "sa demande d'exécution. N'invente aucun fait précis que tu ne peux "
        "pas connaître."
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