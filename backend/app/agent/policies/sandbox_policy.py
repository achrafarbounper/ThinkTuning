"""Policy de sandbox décisionnelle : verdict auto_approve / approve / reject.

Couche PURE (aucune I/O, aucun subprocess) complémentaire de
``ia/tools/sandbox.py`` (qui, elle, fait respecter physiquement : résolution
de chemins, allowlist binaires, sans shell). Séparation des responsabilités :

    - policy (ce module)   : DÉCIDER vite et de façon auditée — classement
      d'une action, détection de cibles sensibles, verdict de policy ;
    - sandbox (ia/tools)   : EXÉCUTER en sécurité — refuse réellement
      l'évasion de racine, l'exécutable interdit, l'injection de shell.

Verdicts (alignés sur ia/agent/approvals.py) :
    REJECT       : cible sensible (``.git``, ``.env``, ``__pycache__``…) avec
                   une catégorie non-read, ou catégorie inconnue sur chemin
                   sensible → bloqué, jamais exécuté, audité ;
    APPROVE      : write/delete/exec/network sur cible non sensible →
                   validation humaine (flux core/approval_store) ;
    AUTO_APPROVE : read/system en lecture → exécution immédiate.

Toute règle dure (REJECT de chemin) n'est PAS désactivable — même
convention que les « règles dures » documentées dans approvals.py.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlparse

from app.domain.entities.plan import Action, ActionCategory, Decision

# Cibles sensibles : tout chemin dont une composante (ou extension) figure
# ici est considérée critique. Ces noms sont volontairement interdits en
# écriture/suppression/exécution par l'agent, quel que soit le réglage.
DENIED_PATH_PARTS = frozenset(
    {
        ".git",
        ".env",
        "__pycache__",
        "venv",
        ".venv",
        "node_modules",
        "id_rsa",
        "id_ed25519",
    }
)

DENIED_EXTENSIONS = frozenset({".env", ".pem", ".key", ".p12", ".pfx"})

# Hôtes privés / loopback (anti-SSRF, aligné sur AGENT_BLOCK_PRIVATE_HOSTS).
_PRIVATE_HOST_PREFIXES = ("10.", "192.168.", "127.", "169.254.")


def classify_path_risk(path: str) -> bool:
    """Vrai si le chemin pointe une cible sensible (partie ou extension)."""
    import os

    parts = [p.lower() for p in os.path.normpath(str(path)).replace("\\", "/").split("/")]
    if any(part in DENIED_PATH_PARTS for part in parts):
        return True
    # id_rsa / id_ed25519 : préfixes de clés privées (id_rsa.pub reste interdit
    # en écriture par prudence ; la lecture publique est un faux positif
    # acceptable pour un backend ML).
    stem = parts[-1] if parts else ""
    ext = os.path.splitext(stem)[1]
    return stem.startswith(("id_rsa", "id_ed25519")) or ext in DENIED_EXTENSIONS


def is_private_host(url: str) -> bool:
    """Vrai si l'URL vise un hôte privé / loopback (anti-SSRF)."""
    try:
        host = urlparse(str(url)).hostname or ""
    except ValueError:
        return True
    if not host:
        return True
    if host in ("localhost", "::1") or host.startswith("0."):
        return True
    return any(host.startswith(prefix) for prefix in _PRIVATE_HOST_PREFIXES)


@lru_cache(maxsize=512)
def classify_tool(tool: str) -> ActionCategory:
    """Classement statique des outils connus (cf. ia/tools/tool_registry.py).

    Cache local : le classement est déterministe, l'appel est fréquent."""
    by_tool: dict[str, ActionCategory] = {
        # lecture
        "read_file": ActionCategory.READ,
        "head_file": ActionCategory.READ,
        "list_dir": ActionCategory.READ,
        "find_file": ActionCategory.READ,
        "file_info": ActionCategory.READ,
        "file_checksum": ActionCategory.READ,
        "count_lines": ActionCategory.READ,
        "read_json": ActionCategory.READ,
        "search_in_files": ActionCategory.READ,
        "tail_file": ActionCategory.READ,
        "now": ActionCategory.READ,
        "gpu_info": ActionCategory.READ,
        "disk_usage": ActionCategory.READ,
        "env_info": ActionCategory.READ,
        "git_status": ActionCategory.READ,
        "git_log": ActionCategory.READ,
        "git_diff": ActionCategory.READ,
        "docker_ps": ActionCategory.READ,
        "docker_logs": ActionCategory.READ,
        "web_search": ActionCategory.NETWORK,
        "web_fetch": ActionCategory.NETWORK,
        "web_read": ActionCategory.NETWORK,
        "http_get": ActionCategory.NETWORK,
        "http_post": ActionCategory.NETWORK,
        "sqlite_query": ActionCategory.READ,
        "postgres_query": ActionCategory.READ,
        # écriture
        "write_file": ActionCategory.WRITE,
        "write_json": ActionCategory.WRITE,
        "append_file": ActionCategory.WRITE,
        "touch": ActionCategory.WRITE,
        "make_dir": ActionCategory.WRITE,
        "copy_path": ActionCategory.WRITE,
        "move_path": ActionCategory.WRITE,
        "split_file": ActionCategory.WRITE,
        "dedupe_lines": ActionCategory.WRITE,
        "unzip_file": ActionCategory.WRITE,
        "zip_path": ActionCategory.WRITE,
        "download_file": ActionCategory.WRITE,
        # suppression
        "remove_path": ActionCategory.DELETE,
        # exécution
        "run_command": ActionCategory.EXEC,
        "run_python": ActionCategory.EXEC,
        "docker_exec": ActionCategory.EXEC,
        # système
        "docker_stats": ActionCategory.SYSTEM,
    }
    return by_tool.get(tool, ActionCategory.UNKNOWN)


# Outils d'accès base de données : verdict selon la première clause SQL
# (SELECT/WITH/EXPLAIN = lecture, tout le reste = rejet — l'agent n'a pas le
# droit de muter les données métier via ses outils génériques).
_DB_TOOLS = frozenset({"sqlite_query", "postgres_query"})
_DB_ALLOWED_PREFIXES = ("select", "with", "explain", "pragma")


def _db_query_is_readonly(args: dict) -> bool:
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return value.strip().lower().startswith(_DB_ALLOWED_PREFIXES)
    return False


def decide(tool: str, args: dict, category: ActionCategory | None = None) -> Decision:
    """Verdict de policy pour une action (pure, déterministe, auditée).

    Args:
        tool:     nom de l'outil (registre ia/tools) ;
        args:     arguments de l'appel ;
        category: catégorie connue, sinon classée via ``classify_tool``.

    Ordre d'évaluation : règles dures (chemins sensibles, SQL mutant, hôte
    privé en POST) → catégories risque (write/delete/exec) → lecture.
    """
    known_category = category is not None and category is not ActionCategory.UNKNOWN
    cat = category if known_category else classify_tool(tool)

    # --- Règles dures : jamais désactivables ---------------------------------
    if tool in _DB_TOOLS and not _db_query_is_readonly(args):
        return Decision.REJECT
    if cat is ActionCategory.NETWORK and tool in ("http_post",):
        url = next((str(v) for v in args.values() if isinstance(v, str) and "://" in v), "")
        if url and is_private_host(url):
            return Decision.REJECT

    # --- Cibles sensibles ------------------------------------------------------
    if cat is not ActionCategory.READ:
        for value in args.values():
            if isinstance(value, str) and value and classify_path_risk(value):
                return Decision.REJECT

    # --- Verdict par catégorie ---------------------------------------------------
    if cat in (ActionCategory.WRITE, ActionCategory.DELETE, ActionCategory.EXEC):
        return Decision.APPROVE
    if tool == "http_post":
        # POST = mutation côté serveur : validation humaine même sur hôte public.
        return Decision.APPROVE
    if cat in (ActionCategory.READ, ActionCategory.SYSTEM, ActionCategory.NETWORK):
        return Decision.AUTO_APPROVE
    return Decision.APPROVE  # UNKNOWN : prudence, validation humaine


def decide_action(action: Action) -> Decision:
    """Variante typée pour une entité ``Action`` du domaine."""
    return decide(action.tool, action.args, action.category)
