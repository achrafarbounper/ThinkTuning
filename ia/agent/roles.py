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
_OPS = [
    "env_info", "disk_usage", "zip_path", "unzip_file",
    "git_status", "git_log", "git_diff", "download_file",
]
_SHELL = ["run_command", "run_python"]
_SYSTEM = ["env_info", "disk_usage", "gpu_info"]
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
}

# Ordre canonique des rôles (pour prompts, plan, validation).
ROLE_ORDER = [
    "web", "files", "ml", "data", "math", "ops", "shell", "docker",
]


def role_names() -> List[str]:
    """Noms de tous les rôles spécialisés (hors « lead » superviseur)."""
    return list(ROLE_ORDER)


def get_role(name: str) -> Optional[Role]:
    """Récupère un rôle par nom (None si inconnu)."""
    return ROLES.get(name)


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