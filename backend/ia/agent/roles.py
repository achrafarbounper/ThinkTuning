"""Registre des rôles spécialisés de l'orchestration multi-agents.

Chaque rôle = un nom, un label, une description, et un SOUS-ENSEMBLE d'outils
du registre central. Un worker créé pour un rôle n'a accès QU'à cet
sous-ensemble (isolation stricte par garde de rôle dans AgentCore).

Les sous-ensembles ci-dessous sont déclaratifs et stables : on référence les
noms d'outils du registre (ia/tools/tool_registry.py). Si un nom n'existe pas,
la résolution (``resolve_role_tools``) le signale au lieu de planter silencieusement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# --- Noms d'outils par famille (réfèrent aux clés du registre) ---
_WEB = ["web_search", "web_fetch", "web_read", "http_get", "http_post"]
_FILES = [
    "list_dir", "read_file", "find_file", "make_dir", "copy_path",
    "move_path", "remove_path", "write_file", "file_info", "file_checksum",
    "head_file", "count_lines", "touch", "write_json", "read_json",
    "find_duplicates", "split_file", "dedupe_lines",
    "search_in_files", "tail_file", "append_file", "now",
]
_ML = [
    "job_list", "job_get", "predict_sentiment", "dataset_stats",
    "model_versions", "start_training", "train_model", "cancel_training",
    "stop_training",
]
_DATA = ["sqlite_query", "postgres_query"]
_MATH = ["calc", "add"]
# Le rôle « ops » porte TOUS les diagnostics lecture seule : env_info / disk_usage
# / gpu_info (aligné sur les outils RÉELS du registre, cf. plan_correct.py qui
# fixe les plans hors-périmètre vers ops). Le bloc _SYSTEM historique est devenu
# un alias de _OPS pour ne pas dupliquer la vérité.
_OPS = [
    "env_info", "disk_usage", "gpu_info",
    "zip_path", "unzip_file",
    "git_status", "git_log", "git_diff", "download_file",
]
_SHELL = ["run_command", "run_python"]
# SCRUM-99 : outils personnalisés d'exemple (shell allowlisté + HTTP générique).
_CUSTOM = ["run_shell", "call_api"]
_SYSTEM = _OPS  # alias conservé (rétro-compatibilité, aucun rôle ne le référence)
_DOCKER = ["docker_ps", "docker_logs", "docker_exec", "docker_stats", "gpu_info"]


@dataclass(frozen=True)
class Role:
    """Définition déclarative d'un rôle spécialisé."""

    name: str
    label: str
    description: str
    tools: List[str] = field(default_factory=list)
    prompt_override: Optional[str] = None
# --- Registre déclaratif des rôles spécialisés ---
ROLES: Dict[str, Role] = {
    "lead": Role(
        name="lead",
        label="Superviseur",
        description=(
            "Rôle de planification et de synthèse. N'exécute AUCUN outil : "
            "il décompose la tâche en sous-tâches assignées aux autres rôles, "
            "puis agrège leurs résultats en une réponse finale cohérente."
        ),
        tools=[],
    ),
    "web": Role(
        name="web",
        label="Recherche web",
        description=(
            "Recherche et lecture d'informations réelles sur Internet "
            "(actualité, faits, contenu de pages)."
        ),
        tools=_WEB,
    ),
    "files": Role(
        name="files",
        label="Fichiers",
        description=(
            "Lecture, écriture et manipulation de fichiers dans la sandbox "
            "(création, édition, déplacement, suppression, recherche)."
        ),
        tools=_FILES,
    ),
    "ml": Role(
        name="ml",
        label="Machine Learning",
        description=(
            "Métier ThinkTuning : jobs d'entraînement, datasets, modèles, "
            "prédictions de sentiment."
        ),
        tools=_ML,
    ),
    "data": Role(
        name="data",
        label="Données (SQL)",
        description=(
            "Interrogation de bases de données relationnelles (SQLite, "
            "PostgreSQL) en lecture sécurisée."
        ),
        tools=_DATA,
    ),
    "math": Role(
        name="math",
        label="Calcul",
        description=(
            "Calculs exacts et expressions mathématiques via la calculatrice sûre."
        ),
        tools=_MATH,
    ),
    "ops": Role(
        name="ops",
        label="Système & Ops",
        description=(
            "Informations système/exploitation, archivage, git, téléchargements."
        ),
        tools=_OPS,
    ),
    "shell": Role(
        name="shell",
        label="Exécution",
        description="Exécution de commandes shell et de scripts Python.",
        tools=_SHELL,
    ),
    "docker": Role(
        name="docker",
        label="Docker & GPU",
        description="Gestion de conteneurs Docker et information GPU.",
        tools=_DOCKER,
    ),
    # --- SCRUM-99 : rôles du pipeline « tools personnalisés » ---------------
    "operator": Role(
        name="operator",
        label="Opérateur tools",
        description=(
            "Exécution contrôlée d'outils personnalisés validés (shell "
            "allowlisté, appels API génériques). N'écrit JAMAIS de code ni "
            "de définition de tool : il consomme le registre."
        ),
        tools=_CUSTOM,
    ),
    "developer": Role(
        name="developer",
        label="Développeur",
        description=(
            "Rédige du code et des définitions de tools DANS SA RÉPONSE "
            "(JSON standard thinktuning.tool/v1). N'exécute AUCUN outil et "
            "ne registre jamais un tool : l'enregistrement est une décision "
            "humaine (review + approbation)."
        ),
        tools=[],
    ),
    "reviewer": Role(
        name="reviewer",
        label="Relecteur sécurité",
        description=(
            "Relit une définition de tool proposée (nom, paramètres, "
            "sécurité, effets de bord) et rend un verdict ARGUMENTÉ. N'a "
            "AUCUN outil : aucun appel d'outil ne peut contourner la relecture."
        ),
        tools=[],
    ),
}

# Ordre canonique des rôles (pour prompts, plan, validation).
ROLE_ORDER = [
    "web", "files", "ml", "data", "math", "ops", "shell", "docker",
    "operator", "developer", "reviewer",
]


def role_names() -> List[str]:
    """Noms de tous les rôles spécialisés (hors « lead » superviseur)."""
    return list(ROLE_ORDER)


def get_role(name: str) -> Optional[Role]:
    """Récupère un rôle par nom (None si inconnu)."""
    return ROLES.get(name)


def role_tools() -> Dict[str, List[str]]:
    """Outils réels par rôle — injectés au prompt du planner (build_planner_prompt).

    Le superviseur (LLM) doit connaître les CAPACITÉS des rôles pour choisir le
    bon rôle : sans cela il choisit « shell » pour des diagnostics que le rôle
    « ops » couvre déjà, ou assigne une lecture à un rôle à risque.
    """
    return {
        name: list(role.tools)
        for name, role in ROLES.items()
        if name != "lead" and role.tools
    }


def resolve_role_tools(name: str, registry: Dict[str, object]) -> Dict[str, object]:
    """Sous-ensemble d'outils réel d'un rôle depuis le registre central.

    Sélectionne dans ``registry`` (ex. TOOLS) les outils déclarés du rôle.
    Un outil déclaré mais absent du registre est ignoré (impossible de
    construire un worker qui appellerait une fonction inexistante).
    """
    role = ROLES.get(name)
    if role is None:
        return {}
    if not role.tools:
        return {}
    return {tool_name: registry[tool_name] for tool_name in role.tools if tool_name in registry}


# --- Politique d'intention par rôle (Approche B : filtrage local) -------------
# Le classifieur global (chat/action) stampé sur chaque PlanTask par
# l'orchestrateur est appliqué au dispatch selon la politique DU RÔLE :
#   - ``pass_through`` : s'exécute quelle que soit l'intention (superviseur…) ;
#   - ``action_only``  : ne s'exécute que pour une intention « action » —
#     un rôle outillé est inutile (et coûteux) pour du pur conversationnel ;
#   - ``chat_first``   : ne s'exécute que pour une intention « chat »
#     (réservé à des rôles rédactionnels futurs : rapport, explication…).
# Ajouter un rôle à politique spécifique = ajouter une ligne ici (Open/Closed),
# sans toucher à la logique de l'orchestrateur.
INTENT_POLICY: Dict[str, str] = {
    "lead": "pass_through",
    "web": "action_only",
    "files": "action_only",
    "ml": "action_only",
    "data": "action_only",
    "math": "action_only",
    "ops": "action_only",
    "shell": "action_only",
    "docker": "action_only",
    # SCRUM-99 : les rôles du pipeline tools sont purement « action » —
    # developer/reviewer n'ont aucun outil et ne servent qu'à produire/juger
    # des définitions ; operator n'a de sens que pour exécuter.
    "operator": "action_only",
    "developer": "action_only",
    "reviewer": "action_only",
}
DEFAULT_INTENT_POLICY = "action_only"

_VALID_INTENT_POLICIES = frozenset({"pass_through", "action_only", "chat_first"})


def intent_decision_for(role: str, intent: str, active: bool = True) -> str:
    """Décision de dispatch d'un worker selon la politique d'intention du rôle.

    Retourne ``"executed"`` ou ``"ignored"``. ``active=False`` (aucun
    classifieur injecté) désactive le filtrage : comportement historique,
    tous les workers s'exécutent.
    """
    if not active:
        return "executed"
    policy = INTENT_POLICY.get(role, DEFAULT_INTENT_POLICY)
    if policy not in _VALID_INTENT_POLICIES:
        return "executed"
    if policy == "pass_through":
        return "executed"
    if policy == "action_only":
        return "executed" if intent == "action" else "ignored"
    # chat_first
    return "executed" if intent == "chat" else "ignored"
