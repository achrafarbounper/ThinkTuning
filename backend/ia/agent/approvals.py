"""Moteur de décision auto_approve / approve / reject de l'agent.

DPE — Décision de Policy d'un appel d'outil. Trois états déterministes :

    - AUTO_APPROVE : action jugée sûre (lecture de fichier, commande non
      sensible, chemins autorisés, réseau en lecture…) → exécutée
      immédiatement, sans validation humaine.
    - APPROVE      : action potentiellement risquée (édition de fichier,
      commande sensible définie par la policy…) → validation humaine AVANT
      toute exécution (via `core.approval_store`, endpoints /api/agent/approvals).
    - REJECT       : action bloquée immédiatement (chemins interdits —
      `.git`, racine sandbox —, binaire dangereux) → jamais exécutée.

Chaque décision est renvoyée sous forme de ``PolicyDecision`` : un JSON
structurellement stable et HORODATÉ (timestamp ISO UTC) qui garantit la
traçabilité et la cohérence du flux.

Surcharge déclarative : un outil peut porter un champ ``approval`` dans
``tools/tools_config.json`` (``"auto"`` | ``"manual"`` | ``"blocked"``) qui
prime sur la classification par défaut, sauf les règles dures (reject de
chemin) qui ne sont jamais désactivables.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional


class Decision(str, Enum):
    """Les trois états du système de décision."""

    AUTO_APPROVE = "auto_approve"
    APPROVE = "approve"
    REJECT = "reject"


# Catégories exposées au dashboard / à la trace (lisible + filtrable).
CATEGORY_READ = "read"
CATEGORY_WRITE = "write"
CATEGORY_DELETE = "delete"
CATEGORY_EXEC = "exec"
CATEGORY_NETWORK = "network"
CATEGORY_SYSTEM = "system"
CATEGORY_UNKNOWN = "unknown"


@dataclass
class PolicyDecision:
    """Décision structurée, stable et horodatée d'un appel d'outil.

    Attributs :
        tool       : nom de l'outil ;
        args       : arguments résumés (secrets / contenus massifs tronqués) ;
        decision   : ``Decision`` (auto_approve / approve / reject) ;
        category   : catégorie lisible de l'action ;
        reason     : justification (affichable au LLM et à l'UI) ;
        args_hash  : empreinte SHA-256 déterministe des arguments ;
        timestamp  : horodatage ISO UTC de la décision.
    """

    tool: str
    args: Dict[str, Any]
    decision: Decision
    category: str
    reason: str
    args_hash: str = ""
    timestamp: str = ""

    def to_dict(self) -> dict:
        """Représentation JSON stable — garantit traçabilité et cohérence."""
        return {
            "tool": self.tool,
            "args": _summary(self.args),
            "decision": self.decision.value,
            "category": self.category,
            "reason": self.reason,
            "args_hash": self.args_hash,
            "timestamp": self.timestamp,
        }


# --- Résumé sûr des arguments -------------------------------------------------------

_SUMMARY_MAX_CHARS = 400


def _summary(args: Any) -> Any:
    """Raccourcit les arguments pour la trace (secrets / contenus tronqués)."""
    if isinstance(args, dict):
        return {key: _summary(val) for key, val in args.items()}
    if isinstance(args, (list, tuple)):
        return [_summary(item) for item in list(args)[:20]]
    if args is None:
        return None
    text = str(args)
    if len(text) > _SUMMARY_MAX_CHARS:
        return text[:_SUMMARY_MAX_CHARS] + "…"
    return text


def _args_hash(args: Dict[str, Any]) -> str:
    """Empreinte SHA-256 déterministe du JSON des arguments."""
    payload = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _timestamp() -> str:
    """Horodatage ISO 8601 UTC (millisecondes)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _utcnow() -> str:
    return _timestamp()


# --- Accès sandbox & registre (convention d'import duale de agent_core) ---------------

try:  # paquet « ia.tools » (imports racinés sur le projet / tests)
    from ..tools.sandbox import (
        get_sandbox_root as _get_sandbox_root,
        safe_resolve as _sandbox_safe_resolve,
    )
except ImportError:  # racine « agent » / « tools » (core/agent_cache.py ajoute ia/)
    from tools.sandbox import (
        get_sandbox_root as _get_sandbox_root,
        safe_resolve as _sandbox_safe_resolve,
    )


def _sandbox_root() -> Path:
    """Racine autorisée des opérations fichiers (relue à chaque appel)."""
    return _get_sandbox_root()


def _safe_resolve(token: str) -> Optional[Path]:
    """Résout un chemin dans la sandbox ; ``None`` si évasion (= interdit)."""
    try:
        return _sandbox_safe_resolve(str(token))
    except (PermissionError, ValueError, OSError):
        return None


# Outils dont un argument « path / filename / src / dst » MUTE le système de
# fichiers : seuls ceux-ci sont soumis aux règles dures (`.git`, racine sandbox).
_MUTATING_PATH_TOOLS = frozenset({
    "write_file", "append_file", "make_dir", "copy_path", "move_path",
    "remove_path", "zip_path", "unzip_file", "download_file",
})

# Binaires jamais exécutables même avec validation humaine (rejet immédiat) :
# ils permettraient d'exécuter n'importe quoi et annuleraient le filtrage.
_BLOCKED_BINARIES = frozenset({
    "cmd", "powershell", "pwsh", "bash", "sh", "zsh", "fish",
    "wscript", "cscript",
})

# Commandes 100 % lecture seule (binaire entier allowlisté pour l'auto-approve).
_READ_COMMANDS = frozenset({"nvidia-smi"})

# Binaires sûrs uniquement pour certaines SOUS-COMMANDES de lecture.
_SAFE_SUBCOMMAND_BINARIES = {
    "git": {"status", "log", "diff", "show", "branch", "rev-parse"},
    "docker": {"ps", "logs", "stats", "images", "inspect", "version"},
    "pip": {"list", "show", "freeze"},
}

# Outils sûrs par nature → AUTO_APPROVE (lecture, réseau en lecture, calculs).
_AUTO_TOOLS = frozenset({
    # fichiers / recherche (lecture)
    "read_file", "list_dir", "find_file", "search_in_files", "tail_file",
    # git / docker / ML / jobs (lecture)
    "git_status", "git_log", "git_diff",
    "docker_ps", "docker_logs", "docker_stats",
    "job_list", "job_get", "model_versions", "dataset_stats",
    "predict_sentiment",
    # réseau en lecture
    "web_search", "web_fetch", "web_read", "http_get",
    # système / mathématiques
    "env_info", "disk_usage", "gpu_info", "now", "calc", "add",
})

# Outils potentiellement risqués → APPROVE (validation humaine avant exécution).
_APPROVE_TOOLS = frozenset({
    # mutations de fichiers
    "write_file", "append_file", "make_dir", "copy_path", "move_path",
    "remove_path", "zip_path", "unzip_file", "download_file",
    # exécution de code / conteneurs / réseau en écriture
    "run_command", "run_python", "docker_exec", "http_post",
    # entraînements (créent un job et écrivent dans experiments/models)
    "start_training", "train_model",
    # arrêts d'entraînements (mutation de l'état d'un job)
    "cancel_training", "stop_training",
})

_DB_QUERY_TOOLS = frozenset({"sqlite_query", "postgres_query"})

# Surcharge déclarative du manifeste (« approval » dans tools_config.json).
_APPROVAL_OVERRIDE_MAP = {
    "auto": Decision.AUTO_APPROVE,
    "manual": Decision.APPROVE,
    "blocked": Decision.REJECT,
}


def _config_approval(tool: str) -> Optional[Decision]:
    """Surcharge déclarative d'un outil (« approval » dans tools_config.json).

    Valeurs reconnues : ``auto`` | ``manual`` | ``blocked``. Absent ou invalide
    → None (la classification par défaut s'applique).
    """
    try:  # même convention duale que agent_core.py
        from ..tools.tool_registry import get_tool_meta
    except ImportError:
        from tools.tool_registry import get_tool_meta
    raw = str(get_tool_meta(tool).get("approval", "")).strip().lower()
    return _APPROVAL_OVERRIDE_MAP.get(raw)


def _is_readonly_query(args: Dict[str, Any]) -> bool:
    """True si la requête SQL reste en lecture seule (défaut = readonly=true)."""
    val = args.get("readonly", True)
    if isinstance(val, str):
        return val.strip().lower() in {"1", "true", "yes", "on"}
    return bool(val)



# --- Matériaux pour le calcul des chemins interdits ------------------------------------

def _path_tokens(args: Dict[str, Any]) -> list[str]:
    """Chemins candidats accessibles dans les arguments (jamais de secret)."""
    tokens = []
    for key in ("path", "filename", "src", "dst"):
        val = args.get(key)
        if isinstance(val, (str,)):
            tokens.append(val)
    return tokens


def _forbidden_path_reason(tool: str, args: Dict[str, Any]) -> Optional[str]:
    """Raison de blocage si un chemin de l'action est interdit, sinon None.

    Règles DURES (jamais surchargeables) pour les outils de mutation : écrire,
    supprimer ou renommer sous ``.git`` ou à la racine de la sandbox est
    toujours bloqué (REJECT), indépendamment du manifeste.
    """
    if tool not in _MUTATING_PATH_TOOLS:
        return None
    root = _sandbox_root()
    for token in _path_tokens(args):
        resolved = _safe_resolve(token)
        if resolved is None:
            return f"Chemin hors sandbox interdit : '{token}'"
        if ".git" in resolved.parts:
            return f"Action interdite sous '.git' : '{token}'"
        if resolved == root:
            return f"Action interdite à la racine de la sandbox : '{token}'"
    return None


def _classify_run_command(tool: str, args: Dict[str, Any]) -> tuple[str, str, Decision]:
    """Classification fine de run_command / run_python.

    - Binaire de lecture seule → auto_approve.
    - `git` avec une sous-commande de lecture → auto_approve.
    - Tout le reste (commande potentiellement mutante / code arbitraire) →
      approve (contrôle humain).
    """
    category = CATEGORY_EXEC
    command = args.get("command", args.get("code"))
    if tool == "run_command":
        if not isinstance(command, (list, tuple)) or not command:
            return category, "commande invalide — classification prudente", Decision.APPROVE
        exe = str(command[0]).lower()
        if exe.endswith(".exe"):
            exe = exe[:-4]
        if exe in _BLOCKED_BINARIES:
            return category, f"binaire interdit par la policy : « {exe} »", Decision.REJECT
        args_list = [str(c).lower() for c in command[1:]]
        if exe in _READ_COMMANDS:
            return category, "commande de lecture seule (binaire allowlist)", Decision.AUTO_APPROVE
        subcommands = _SAFE_SUBCOMMAND_BINARIES.get(exe)
        if subcommands is not None and args_list and args_list[0] in subcommands:
            return category, f"commande `{exe}` de lecture", Decision.AUTO_APPROVE
        return category, "commande potentiellement mutante — validation humaine", Decision.APPROVE
    # run_python : code arbitraire, toujours approve par défaut.
    return category, "exécution de code arbitraire — validation humaine", Decision.APPROVE


def _reason_for(decision: Decision, tool: str, category: str) -> str:
    reasons = {
        Decision.AUTO_APPROVE: (
            f"Action sûre ({category}) exécutée automatiquement : {tool}"
        ),
        Decision.APPROVE: (
            f"Action potentiellement risquée ({category}) nécessite validation humaine : {tool}"
        ),
        Decision.REJECT: (
            f"Action bloquée par la policy (aucune exécution) : {tool}"
        ),
    }
    return reasons[decision]


def _registry_approval(tool: str) -> Optional[Decision]:
    """Surcharge « registry » (SCRUM-99) : approval effectif d'un tool enregistré.

    Les tools DYNAMIQUES (proposés par le planner, relus par un humain) portent
    leur classification DANS la ToolRegistry (dérivée de ``safety`` à
    l'enregistrement, cf. ``ia/tools/registry.py``) :

        - ``manual``  (défaut fail-closed) → APPROVE (validation humaine) ;
        - ``blocked`` (``safety.level = dangerous``) → REJECT immédiat ;
        - ``auto``    → AUTO_APPROVE, uniquement si la source était humaine
          (``allow_auto_approval`` — sinon la registry force ``manual``).

    Les tools natifs ne sont pas concernés (retour ``None`` : leur politique
    reste celle du manifeste / des listes statiques ci-dessous).
    """
    try:  # import tardif : approvals doit rester léger et ne jamais casser le gate
        from ..tools.registry import get_global_registry
    except ImportError:
        try:
            from tools.registry import get_global_registry  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        registered = get_global_registry().get_tool(tool)
    except Exception:  # noqa: BLE001 — le gate ne doit jamais planter ici
        return None
    if registered is None or not registered.dynamic:
        return None
    value = (registered.approval or "").lower()
    if value == "auto":
        return Decision.AUTO_APPROVE
    if value == "manual":
        return Decision.APPROVE
    if value == "blocked":
        return Decision.REJECT
    return None


def classify(tool: str, args: Dict[str, Any]) -> PolicyDecision:
    """Classe un appel d'outil et renvoie la décision structurée et horodatée.

    Ordre d'évaluation (du plus fort au plus souple) :
        1. règles dures : chemin interdit (`.git`, racine sandbox) → REJECT ;
        2. surcharge déclarative du manifeste (``approval``) ;
        2 bis. surcharge « registry » : tool dynamique (SCRUM-99) dont
          l'``approval`` dérive de ``safety`` (manual/blocked/auto) ;
        3. exécution de code (`run_command` / `run_python`) → fine grade ;
        4. listes statiques auto_approve / approve ;
        5. défaut = APPROVE (échec fermé : tout outil inconnu exige l'humain).
    """
    args = dict(args or {})
    hash_value = _args_hash(args)
    ts = _timestamp()

    def _badge(decision, category, reason) -> PolicyDecision:
        return PolicyDecision(
            tool=tool,
            args=args,
            decision=decision,
            category=category,
            reason=reason,
            args_hash=hash_value,
            timestamp=ts,
        )

    # 1. Règles dures (chemin interdit) — toujours REJECT.
    forbidden = _forbidden_path_reason(tool, args)
    if forbidden:
        category = CATEGORY_DELETE if tool in ("remove_path", "move_path") else CATEGORY_WRITE
        return _badge(Decision.REJECT, category, forbidden)

    # 2. Surcharge déclarative du manifeste (prima).
    override = _config_approval(tool)
    if override is not None:
        category = _category_of(tool)
        return _badge(override, category, _reason_for(override, tool, category))

    # 2 bis. Surcharge « registry » (tools dynamiques SCRUM-99) : la
    # classification dérivée du standard thinktuning.tool/v1 (safety →
    # approval à l'enregistrement) prime sur toute heuristique par défaut.
    registry_decision = _registry_approval(tool)
    if registry_decision is not None:
        category = _category_of(tool)
        return _badge(
            registry_decision, category,
            _reason_for(registry_decision, tool, category),
        )

    # 3. Exécution de code — classification fine.
    if tool in ("run_command", "run_python"):
        category, reason, decision = _classify_run_command(tool, args)
        return _badge(decision, category, reason)

    # 3 bis. Requêtes SQL : lecture seule par défaut (garde dure côté outils via
    # PRAGMA query_only) → auto ; écriture explicite → validation humaine.
    if tool in _DB_QUERY_TOOLS:
        category = CATEGORY_READ if _is_readonly_query(args) else CATEGORY_WRITE
        if category == CATEGORY_READ:
            return _badge(
                Decision.AUTO_APPROVE, category,
                "requête SQL en lecture seule (readonly=true)",
            )
        return _badge(
            Decision.APPROVE, category,
            "requête SQL en écriture (readonly=false) — validation humaine",
        )

    # 4. Listes statiques.
    if tool in _AUTO_TOOLS:
        return _badge(Decision.AUTO_APPROVE, _category_of(tool), _reason_for(Decision.AUTO_APPROVE, tool, _category_of(tool)))
    if tool in _APPROVE_TOOLS:
        return _badge(Decision.APPROVE, _category_of(tool), _reason_for(Decision.APPROVE, tool, _category_of(tool)))

    # 5. Défaut prudent.
    return _badge(Decision.APPROVE, CATEGORY_UNKNOWN,
                  f"Outil « {tool} » sans politique explicite — validation humaine")


def _category_of(tool: str) -> str:
    """Catégorie lisible d'un outil (pour l'UI / la trace)."""
    if tool in ("remove_path", "move_path"):
        return CATEGORY_DELETE
    if tool in ("copy_path", "write_file", "write_json", "append_file", "touch",
                "make_dir", "dedupe_lines", "split_file"):
        return CATEGORY_WRITE
    if tool in ("run_command", "run_python", "docker_exec", "run_shell"):
        return CATEGORY_EXEC
    if tool in ("web_search", "web_fetch", "web_read", "http_get", "http_post",
                "download_file", "call_api"):
        return CATEGORY_NETWORK
    if tool in ("env_info", "disk_usage", "gpu_info", "now"):
        return CATEGORY_SYSTEM
    return CATEGORY_READ


# --- Alias historiques utilisés par le gate de agent_core ------------------------------

ApprovalDecision = Decision  # même énumération à trois états
classify_approval = classify  # même signature (tool, args) -> PolicyDecision