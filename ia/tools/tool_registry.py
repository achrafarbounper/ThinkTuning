"""Registre central des outils de l'agent.

TOOLS       : nom -> fonction appelée par AgentCore / POST /tools/run.
REQUIRED_ARGS : arguments obligatoires validés AVANT l'appel (les autres
              paramètres des fonctions ont des valeurs par défaut).

Ajouter un outil = créer sa fonction dans un module de ia/tools/, puis une
ligne ici + sa ligne REQUIRED_ARGS. Le system_prompt et l'API (/health,
/tools) se mettent à jour via ces deux dicts.
"""

from tools.math_tools import add
from tools.file_tools import write_file
from tools.system_tools import (
    copy_path,
    list_dir,
    make_dir,
    move_path,
    read_file,
    remove_path,
)
from tools.shell_tools import run_command, run_python
from tools.network_tools import http_get, http_post
from tools.docker_tools import docker_exec, docker_logs, docker_ps
from tools.gpu_tools import gpu_info
from tools.database_tools import postgres_query, sqlite_query

TOOLS = {
    # math
    "add": add,
    # fichiers (sandbox)
    "write_file": write_file,
    "list_dir": list_dir,
    "read_file": read_file,
    "make_dir": make_dir,
    "copy_path": copy_path,
    "move_path": move_path,
    "remove_path": remove_path,
    # exécution
    "run_command": run_command,
    "run_python": run_python,
    # réseau
    "http_get": http_get,
    "http_post": http_post,
    # docker
    "docker_ps": docker_ps,
    "docker_logs": docker_logs,
    "docker_exec": docker_exec,
    # gpu
    "gpu_info": gpu_info,
    # bases de données
    "sqlite_query": sqlite_query,
    "postgres_query": postgres_query,
}

REQUIRED_ARGS = {
    "add": ["a", "b"],
    "write_file": ["filename", "content"],
    "list_dir": [],
    "read_file": ["path"],
    "make_dir": ["path"],
    "copy_path": ["src", "dst"],
    "move_path": ["src", "dst"],
    "remove_path": ["path"],
    "run_command": ["command"],
    "run_python": ["code"],
    "http_get": ["url"],
    "http_post": ["url"],
    "docker_ps": [],
    "docker_logs": ["container"],
    "docker_exec": ["container", "command"],
    "gpu_info": [],
    "sqlite_query": ["db_path", "query"],
    "postgres_query": ["query"],
}
