"""Registre central des outils de l'agent.

TOOLS       : nom -> fonction appelée par AgentCore / POST /tools/run.
REQUIRED_ARGS : arguments obligatoires validés AVANT l'appel (les autres
              paramètres des fonctions ont des valeurs par défaut).

Ajouter un outil = créer sa fonction dans un module de ia/tools/, puis une
ligne ici + sa ligne REQUIRED_ARGS. Le system_prompt et l'API (/health,
/tools) se mettent à jour via ces deux dicts.
"""

# Imports relatifs : fonctionnent à la fois sous le paquet « ia.tools » (tests :
# from ia.agent.agent_core import ...) et sous la racine « tools » (runtime :
# core/agent_cache.py ajoute ia/ au sys.path puis importe tools.tool_registry).
from .math_tools import add
from .file_tools import edit_file, write_file
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
    dataset_stats,
    job_get,
    job_list,
    model_versions,
    predict_sentiment,
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

TOOLS = {
    # math
    "add": add,
    # calculatrice sûre
    "calc": calc,
    # fichiers (sandbox)
    "write_file": write_file,
    "edit_file": edit_file,
    "list_dir": list_dir,
    "read_file": read_file,
    "find_file": find_file,
    "make_dir": make_dir,
    "copy_path": copy_path,
    "move_path": move_path,
    "remove_path": remove_path,
    # recherche / lecture ciblée
    "search_in_files": search_in_files,
    "tail_file": tail_file,
    "append_file": append_file,
    "now": now,
    # exécution
    "run_command": run_command,
    "run_python": run_python,
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
}

REQUIRED_ARGS = {
    "add": ["a", "b"],
    "calc": ["expression"],
    "write_file": ["filename", "content"],
    "edit_file": ["path", "old_text", "new_text"],
    "list_dir": [],
    "read_file": ["path"],
    "find_file": ["pattern"],
    "make_dir": ["path"],
    "copy_path": ["src", "dst"],
    "move_path": ["src", "dst"],
    "remove_path": ["path"],
    "search_in_files": ["pattern"],
    "tail_file": ["path"],
    "append_file": ["path", "content"],
    "now": [],
    "run_command": ["command"],
    "run_python": ["code"],
    "http_get": ["url"],
    "http_post": ["url"],
    "download_file": ["url", "filename"],
    "web_search": ["query"],
    "web_fetch": ["url"],
    "web_read": ["url"],
    "docker_ps": [],
    "docker_logs": ["container"],
    "docker_exec": ["container", "command"],
    "docker_stats": [],
    "gpu_info": [],
    "env_info": [],
    "disk_usage": [],
    "zip_path": ["src", "dst"],
    "unzip_file": ["src", "dst"],
    "git_status": [],
    "git_log": [],
    "git_diff": [],
    "sqlite_query": ["db_path", "query"],
    "postgres_query": ["query"],
    "job_list": [],
    "job_get": ["job_id"],
    "predict_sentiment": ["texts"],
    "dataset_stats": ["path"],
    "model_versions": [],
}
