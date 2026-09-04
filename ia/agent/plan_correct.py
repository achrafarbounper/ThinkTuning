"""Correcteur DÉTERMINISTE du plan produit par le superviseur (planner hybride).

Le LLM propose un plan ; ce module applique des règles métier dures AVANT la
validation structurelle (``plan_validator``), qui reste la dernière barrière.

Règles implémentées (aucune dépendance LLM) :
    - diagnostics système / lecture seule → rôle « ops » (env_info, disk_usage,
      gpu_info) ; JAMAIS « shell » pour du CPU simple ;
    - « shell » : autorisé UNIQUEMENT si la sous-tâche référence une commande
      whitelistée ; sinon rejet immédiat (``PlanRejected``) ;
    - SQL : seules les lectures passent (SELECT/WITH/EXPLAIN/PRAGMA) ; toute
      mutation SQL est un rejet immédiat (jamais soumise à un worker) ;
    - recherches / lecture web → rôle « web » ;
    - lecture seule → marquage ``auto_approve`` (pas de carte d'approbation).
"""

from __future__ import annotations

import re
from typing import Any

_DIAGNOSTIC_MARKERS = re.compile(
    r"(diagnostic|version|environnement|env ?_?info|cpu|ram|m[ée]moire|disque|"
    r"disk|gpu|python version|syst[èe]me|os ?info|plateforme|stockage|"
    r"utilisat(ion|eurs?)|processus|ressources?|r[ée]seau|hostname|ping)",
    re.IGNORECASE,
)

_WEB_MARKERS = re.compile(
    r"(cherche sur (le web|internet)|recherche web|web|site internet|page web|"
    r"url|http|actualit[ée]|recherche sur internet|consulte le site|"
    r"lis la page)",
    re.IGNORECASE,
)

_SQL_MUTATION = re.compile(
    r"\b(insert\s+into|update\s+|delete\s+from|drop\s+(table|database|index)"
    r"|alter\s+table|create\s+(table|database|index)|truncate\s+table|"
    r"replace\s+into|grant\s+|revoke\s+|exec\s*\(|merge\s+into)\b",
    re.IGNORECASE,
)

_SQL_READ_TERMS = re.compile(r"\b(select|with|explain|pragma)\b", re.IGNORECASE)

# Commandes whitelistées pour le rôle shell — la commande doit être en POSITION
# de commande : en tête de sous-tâche, après « : » / guillemets, ou juste après
# un verbe d'exécution (évite les faux positifs du français : « du », « le »…).
_SHELL_WHITELIST = re.compile(
    r"(?:^|[:»«\"']|"
    r"\b(?:ex[ée]cute[rz]?|lance[rz]?|run|tape[rz]?)\s+"
    r"(?:la\s+|le\s+|une\s+|un\s+|cette\s+)?(?:commandes?\s+)?)"
    r"\s*(git(?:\s+(?:status|log|diff|branch|remote))?|ls|cat|df|free|top|ps|"
    r"nvidia-smi|python3?|pip|pwd|echo|uname|whoami|du|find|head|tail|grep|wc|"
    r"date|uptime|ip)\b",
    re.IGNORECASE,
)

_FILE_WRITE_MARKERS = re.compile(
    r"(cr[ée]e( un| le)? fichier|[ée]cris( dans| dans le| le)? fichier|"
    r"modifie( le)? fichier|supprime( le)? fichier|renomme|d[ée]place|copie|"
    r"enregistre|sauvegarde|cr[ée]e( le)? dossier|extrais l.archives?|zip)",
    re.IGNORECASE,
)

_DANGEROUS_SHELL = re.compile(
    r"\b(rm\s+-rf|mkfs|dd\s+if=|shutdown|reboot|format|:\(\)|curl\s+.*\|.*sh|"
    r"chmod\s+777|sudo\s+|wget\s+.*-O\s+/|>.*/etc/)\b",
    re.IGNORECASE,
)


class PlanRejected(ValueError):
    """Le plan contient une sous-tâche qui viole une règle dure.

    Levé pendant ``correct_plan`` → l'orchestrateur aborde le run (jamais de
    dispatch d'une action non conforme : shell non whitelisté, SQL mutant).
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)
def _classify(subtask: str) -> str:
    """Catégorie métier d'une sous-tâche (heuristique déterministe).

    Ordre d'évaluation : SQL (mutation d'abord) → DANGER shell → INTENTION
    d'exécution (commande/script/terminal) → diagnostic → web → mutation de
    fichiers → général. L'intention d'exécution prime sur le sujet : « exécute
    un script de nettoyage système » est du SHELL (à whitelist), pas un
    diagnostic.
    """
    text = (subtask or "").strip()
    if not text:
        return "empty"
    if _SQL_MUTATION.search(text):
        return "sql_mutant"
    if _SQL_READ_TERMS.search(text) or re.search(
        r"\b(sql|base de donn[ée]es|table)\b", text, re.IGNORECASE
    ):
        return "sql_read"
    if _DANGEROUS_SHELL.search(text):
        return "shell_dangerous"
    if re.search(r"\b(commandes?|shell|terminal|cmd|script)\b", text, re.IGNORECASE):
        return "shell"
    if _DIAGNOSTIC_MARKERS.search(text):
        return "diagnostic"
    if _WEB_MARKERS.search(text):
        return "web"
    if _FILE_WRITE_MARKERS.search(text):
        return "file_write"
    return "general"


def _shell_whitelisted(subtask: str) -> bool:
    """Vrai si la sous-tâche shell référence une commande whitelistée."""
    return bool(_SHELL_WHITELIST.search(subtask or ""))


def _is_read_only_task(task: dict[str, Any]) -> bool:
    """Vrai pour une sous-tâche de pure lecture (jamais de carte d'approbation)."""
    role = str(task.get("role") or "")
    category = _classify(str(task.get("subtask") or ""))
    if role in ("data",) and category == "sql_read":
        return True
    if role in ("ops", "web") and category in ("diagnostic", "web", "general"):
        # ops/web : outils du rôle majoritairement READ → auto-approve.
        return True
    return False


def correct_plan(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Corrige le plan proposé par le LLM (liste de dicts), via copies.

    Lève ``PlanRejected`` si une sous-tâche viole une règle dure.
    """
    corrected: list[dict[str, Any]] = []
    for task in tasks or []:
        role = str(task.get("role") or "").strip()
        subtask = str(task.get("subtask") or "").strip()
        new_task = dict(task)
        new_task["subtask"] = subtask
        category = _classify(subtask)

        # --- SQL : lecture seulement, mutation = rejet immédiat -----------------
        if category == "sql_mutant":
            raise PlanRejected(
                f"Sous-tâche {task.get('task_id', '?')} : SQL mutant interdit "
                f"(« {subtask[:120]} »). Les workers data ne font que des lectures."
            )
        if category == "sql_read":
            new_task["role"] = "data"
            new_task["auto_approve"] = True
            corrected.append(new_task)
            continue

        # --- Diagnostics : ops, jamais shell pour du CPU simple -----------------
        if category == "diagnostic":
            if role in ("shell", "files", "web", "math", "ml"):
                new_task["role"] = "ops"
            new_task["auto_approve"] = True
            corrected.append(new_task)
            continue

        # --- Web (lecture : auto-approve sauf intention de publication) ----------
        if category == "web":
            if role == "ops" and not _DIAGNOSTIC_MARKERS.search(subtask):
                new_task["role"] = "web"  # « cherche sur le web » ≠ diagnostic
            if not re.search(r"\b(post|publie[rz]?|envoie[rz]?|soumet)\b", subtask,
                             re.IGNORECASE):
                new_task["auto_approve"] = True  # web_search/web_read/http_get = READ
            corrected.append(new_task)
            continue

        # --- Shell : commande whitelistée requise --------------------------------
        if role == "shell" or category in ("shell", "shell_dangerous"):
            if category == "shell_dangerous" or not _shell_whitelisted(subtask):
                raise PlanRejected(
                    f"Sous-tâche {task.get('task_id', '?')} : rôle « shell » "
                    f"réservé aux commandes whitelistées (« {subtask[:120]} »)."
                )
            corrected.append(new_task)
            continue

        # --- Lecture seule : auto-approve -----------------------------------------
        if _is_read_only_task(new_task):
            new_task["auto_approve"] = True
        corrected.append(new_task)

    return corrected


def note_auto_approved(tasks: list[dict[str, Any]]) -> list[str]:
    """Liste des task_id marqués auto-approve (observabilité / réduction approvals)."""
    return [str(t.get("task_id")) for t in tasks or [] if t.get("auto_approve")]
