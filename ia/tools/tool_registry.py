"""Registre central des outils de l'agent.

TOOLS       : nom -> fonction appelée par AgentCore / POST /tools/run.
REQUIRED_ARGS : arguments obligatoires validés AVANT l'appel (les autres
              paramètres des fonctions ont des valeurs par défaut).
TOOL_META   : les MÉTADONNÉES (description, paramètres, args requis) chargées
              depuis tools_config.json — source unique déclarative des outils.

Deux parties bien distinctes :
  1. La DÉFINITION déclarative -> ia/tools/tools_config.json
     (name, description, required_args, parameters). C'est le seul endroit que
     l'on édite pour décrire un outil au LLM / à l'API.
  2. L'IMPLÉMENTATION exécutable -> les fonctions Python de ia/tools/*.py,
     référencées ci-dessous dans TOOLS (un JSON ne contient pas de logique :
     on décrit un outil en JSON, mais on l'exécute en code).

Ajouter un outil :
  - créer sa fonction dans un module de ia/tools/
  - l'enregistrer dans TOOLS ci-dessous
  - ajouter son entrée dans tools_config.json (description, required_args,
    parameters). Un test anti-divergence vérifie la cohérence entre le JSON,
    TOOLS et REQUIRED_ARGS.
"""

import json
from pathlib import Path

# Imports relatifs : fonctionnent à la fois sous le paquet « ia.tools » (tests :
# from ia.agent.agent_core import ...) et sous la racine « tools » (runtime :
# core/agent_cache.py ajoute ia/ au sys.path puis importe tools.tool_registry).
from .math_tools import add
from .file_tools import (
    count_lines,
    dedupe_lines,
    file_checksum,
    file_info,
    find_duplicates,
    head_file,
    read_json,
    split_file,
    touch,
    write_file,
    write_json,
)
from .system_tools import (
    copy_path,
    find_file,
    list_dir,
    make_dir,
    move_path,
    read_file,
    remove_path,
)
from .shell_tools import run_command, run_python
from .network_tools import http_get, http_post
from .web_tools import web_search, web_fetch, web_read
from .docker_tools import docker_exec, docker_logs, docker_ps
from .gpu_tools import gpu_info
from .database_tools import postgres_query, sqlite_query
from .search_tools import append_file, now, search_in_files, tail_file
from .calc_tools import calc
from .ml_tools import (
    cancel_training,
    dataset_stats,
    job_get,
    job_list,
    model_versions,
    predict_sentiment,
    start_training,
    stop_training,
    train_model,
)
from .ops_tools import (
    disk_usage,
    download_file,
    env_info,
    git_diff,
    git_log,
    git_status,
    unzip_file,
    zip_path,
)
from .ops_tools import docker_stats
from .custom_tools import call_api, run_shell  # SCRUM-99 (tools d'exemple)

TOOLS = {
    # math
    "add": add,
    # calculatrice sûre
    "calc": calc,
    # fichiers (sandbox)
    "write_file": write_file,
    "list_dir": list_dir,
    "read_file": read_file,
    "find_file": find_file,
    "make_dir": make_dir,
    "copy_path": copy_path,
    "move_path": move_path,
    "remove_path": remove_path,
    # fichiers : inspection / production avancée
    "file_info": file_info,
    "file_checksum": file_checksum,
    "head_file": head_file,
    "count_lines": count_lines,
    "touch": touch,
    "write_json": write_json,
    "read_json": read_json,
    "find_duplicates": find_duplicates,
    "split_file": split_file,
    "dedupe_lines": dedupe_lines,
    # recherche / lecture ciblée
    "search_in_files": search_in_files,
    "tail_file": tail_file,
    "append_file": append_file,
    "now": now,
    # exécution
    "run_command": run_command,
    "run_python": run_python,
    # tools personnalisés d'exemple (SCRUM-99) : shell allowlisté + HTTP générique
    "run_shell": run_shell,
    "call_api": call_api,
    # réseau
    "http_get": http_get,
    "http_post": http_post,
    "download_file": download_file,
    # internet (recherche + fetch + lecture)
    "web_search": web_search,
    "web_fetch": web_fetch,
    "web_read": web_read,
    # docker
    "docker_ps": docker_ps,
    "docker_logs": docker_logs,
    "docker_exec": docker_exec,
    "docker_stats": docker_stats,
    # gpu
    "gpu_info": gpu_info,
    # système / exploitation
    "env_info": env_info,
    "disk_usage": disk_usage,
    "zip_path": zip_path,
    "unzip_file": unzip_file,
    "git_status": git_status,
    "git_log": git_log,
    "git_diff": git_diff,
    # bases de données
    "sqlite_query": sqlite_query,
    "postgres_query": postgres_query,
    # métier ThinkTuning
    "job_list": job_list,
    "job_get": job_get,
    "predict_sentiment": predict_sentiment,
    "dataset_stats": dataset_stats,
    "model_versions": model_versions,
    "start_training": start_training,
    "train_model": train_model,
    "cancel_training": cancel_training,
    "stop_training": stop_training,
}

# ---------------------------------------------------------------------------
# Métadonnées déclaratives chargées depuis tools_config.json (source unique).
# REQUIRED_ARGS est dérivé du JSON : on ne le maintient plus à la main.
# ---------------------------------------------------------------------------
_MANIFEST_PATH = Path(__file__).resolve().parent / "tools_config.json"

with _MANIFEST_PATH.open(encoding="utf-8") as _fh:
    _MANIFEST = json.load(_fh)["tools"]

TOOL_META = {name: meta for name, meta in _MANIFEST.items()}


def get_tool_meta(name: str) -> dict:
    """Métadonnées déclaratives d'un outil ({} si absent du manifeste)."""
    return TOOL_META.get(name, {})


def required_args_of(name: str) -> list[str]:
    """Args obligatoires déclarés du JSON pour un outil."""
    return get_tool_meta(name).get("required_args", [])


# Dérivé : un clé manquante dans le JSON est une source de divergence -> le
# test anti-divergence échoue, plutôt que de produire un prompt incomplet.
REQUIRED_ARGS = {name: meta.get("required_args", []) for name, meta in TOOL_META.items()}
