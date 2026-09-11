"""Outils d'exécution de l'agent : commandes externes et code Python.

Sécurité :
    - `run_command` : LISTE d'arguments (jamais de shell -> pas d'injection),
      allowlist de binaires (AGENT_ALLOWED_BINARIES), timeout obligatoire,
      sorties plafonnées. Les shells (cmd/powershell/bash/sh) sont volontairement
      exclus de l'allowlist par défaut.
    - `run_python` : le code est écrit dans un fichier temporaire sous la
      sandbox puis exécuté dans un SOUS-PROCESSUS isolé (isolation réelle,
      contrairement à exec/eval), avec timeout et nettoyage.
"""

import ast
import os
import sys
import uuid
from pathlib import Path
from typing import Callable

from .sandbox import (
    check_command_allowed,
    get_sandbox_root,
    run_subprocess,
    safe_resolve,
    truncate_output,
)

DEFAULT_COMMAND_TIMEOUT_S = 60.0
DEFAULT_PYTHON_TIMEOUT_S = 30.0

# --- Confinement run_python (P1 point 9) ----------------------------------------
# Imports réseau/sous-processus/interpréteur interdits dans un snippet. `os`
# reste autorisé (trop de snippets légitimes en dépendent) — seuls ses appels
# d'exécution (system/popen/exec*/spawn*/startfile) sont bloqués.
_BLOCKED_IMPORTS = frozenset(
    {
        "socket", "urllib", "requests", "httpx", "http", "subprocess", "pty",
        "ftplib", "telnetlib", "smtplib", "importlib",
    }
)

# Noms d'appels interdits quelle que soit leur provenance (y compris via
# `from os import system`).
_BLOCKED_CALL_NAMES = frozenset(
    {"system", "popen", "startfile", "exec", "eval", "compile", "__import__"}
)

# Attributs système d'exécution (os.system, os.popen, os.exec*, os.spawn*, …).
_OS_EXEC_ATTRS = frozenset(
    {
        "system", "popen", "startfile",
        "execv", "execl", "execvp", "execle", "execve", "execvpe", "execlp", "execlpe",
        "spawnl", "spawnv", "spawnve", "spawnvpe", "posix_spawn",
    }
)

# Variables NON sensibles héritées du process parent vers le snippet. Aucune
# clé API / token / DSN / mot de passe n'y figure : seules les variables
# nécessaires à la bibliothèque standard et aux caches sont transmises.
_ALLOWED_ENV_KEYS = (
    "PATH", "SYSTEMROOT", "HOME", "USERPROFILE",
    "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "LC_CTYPE",
)


def _scan_snippet_for_unsafe(code: str) -> None:
    """Analyse AST du snippet : rejette imports réseau/sous-processus et
    exécution dynamique (défense en profondeur, P1 point 9).

    Lève ``PermissionError`` avec le détail (module/appel interdit). Ce scan
    est une ceinture-bretelles : la vraie frontière reste l'environnement
    minimal (aucun secret) + ``python -I``. Un conteneur par snippet (microVM/
    gVisor) serait la version complète — P2 documenté.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"Snippet Python invalide : {exc}") from exc

    blocked_imports: list[str] = []
    blocked_calls: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            blocked_imports.extend(
                alias.name
                for alias in node.names
                if alias.name.split(".")[0] in _BLOCKED_IMPORTS
            )
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in _BLOCKED_IMPORTS:
                blocked_imports.append(node.module)
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in _BLOCKED_CALL_NAMES:
                    blocked_calls.append(func.id)
            elif isinstance(func, ast.Attribute):
                if (
                    func.attr in _OS_EXEC_ATTRS
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "os"
                ):
                    blocked_calls.append(f"os.{func.attr}")
                elif (
                    func.attr in {"import_module", "reload"}
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "importlib"
                ):
                    blocked_calls.append(f"importlib.{func.attr}")

    if blocked_imports or blocked_calls:
        detail = "; ".join(sorted(set(blocked_imports)) + sorted(set(blocked_calls)))
        raise PermissionError(
            "Snippet refusé (confinement P1) : imports/appels interdits — "
            f"{detail}. Réseau et sous-processus sont bloqués, et le snippet "
            "ne reçoit aucune variable d'environnement sensible."
        )


def _snippet_env() -> dict[str, str]:
    """Environnement MINIMAL d'un snippet : jamais copié depuis os.environ.

    Seules les clés non sensibles d'``_ALLOWED_ENV_KEYS`` sont héritées, plus
    les drapeaux Python (pas de .pyc, stdout non bufferisé).
    """
    env = {key: os.environ[key] for key in _ALLOWED_ENV_KEYS if key in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _snippet_preexec() -> Callable[[], None] | None:
    """Fonction preexec bornant CPU/mémoire du snippet (POSIX uniquement).

    ``resource`` n'existe pas sous Windows (ni sous certains interpréteurs) :
    retourne ``None`` -> aucun rlimit, le timeout `run_subprocess` (défaut
    30 s) reste l'arbitre du temps d'exécution.
    """
    try:
        import resource  # type: ignore[import-not-found]  # POSIX only
    except ImportError:
        return None

    def _apply_limits() -> None:
        cpu_cap = int(DEFAULT_PYTHON_TIMEOUT_S) + 5
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_cap, cpu_cap + 5))
        address_cap = 1024 * 1024 * 1024  # 1 GiB d'espace d'adressage
        resource.setrlimit(resource.RLIMIT_AS, (address_cap, address_cap))

    return _apply_limits

from .sandbox import (
    check_command_allowed,
    get_sandbox_root,
    run_subprocess,
    safe_resolve,
    truncate_output,
)

DEFAULT_COMMAND_TIMEOUT_S = 60.0
DEFAULT_PYTHON_TIMEOUT_S = 30.0


# --- Commandes externes ----------------------------------------------------------
def run_command(command: list, timeout: float = DEFAULT_COMMAND_TIMEOUT_S,
                cwd: str | None = None) -> dict:
    """Exécute une commande en liste d'arguments, ex : ["git", "--version"].

    L'exécutable (premier élément) doit figurer dans l'allowlist
    (AGENT_ALLOWED_BINARIES). `cwd` optionnel, confiné à la sandbox.
    """
    if isinstance(command, str):
        raise ValueError(
            "'command' doit être une LISTE (ex: [\"git\", \"--version\"]), "
            "pas une chaîne — cela évite toute injection de shell."
        )
    check_command_allowed(command)
    working_dir = safe_resolve(cwd, must_exist=True) if cwd else None

    code, out, err = run_subprocess(
        command, timeout=max(1.0, min(float(timeout), 600.0)), cwd=working_dir
    )
    return {
        "command": [str(c) for c in command],
        "returncode": code,
        "stdout": out,
        "stderr": err,
    }


# --- Code Python isolé -------------------------------------------------------------
def run_python(code: str, timeout: float = DEFAULT_PYTHON_TIMEOUT_S) -> dict:
    """Exécute un extrait Python dans un sous-processus fraîchement créé.

    Confinement P1 (point 9) :
        - LANCEMENT ISOLÉ : ``python -I -E`` (ignore PYTHONPATH, PYTHONSTARTUP,
          PYTHONHOME et les modules user-site) ;
        - ENVIRONNEMENT MINIMAL (``_snippet_env``) : aucun secret hérité
          (API_KEY, OPENROUTER_API_KEY, HF_TOKEN, DSN…) — toute exfiltration
          par héritage disparaît ;
        - BORNES CPU/mémoire via ``resource.setrlimit`` (POSIX uniquement ;
          no-op sous Windows où `resource` n'existe pas) ;
        - SCAN AST (``_scan_snippet_for_unsafe``) : imports réseau /
          sous-processus et exécution dynamique refusés (défense en profondeur).

    Le script vit sous `<sandbox>/.agent_tmp/` et est supprimé après exécution.
    stdout/stderr capturés et plafonnés.
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("'code' doit être une chaîne Python non vide.")

    _scan_snippet_for_unsafe(code)

    tmp_dir = get_sandbox_root() / ".agent_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    script = tmp_dir / f"snippet_{uuid.uuid4().hex}.py"
    script.write_text(code, encoding="utf-8")

    try:
        proc_code, out, err = run_subprocess(
            [sys.executable, "-I", "-E", str(script)],
            timeout=max(1.0, min(float(timeout), 300.0)),
            cwd=get_sandbox_root(),
            env=_snippet_env(),
            preexec_fn=_snippet_preexec(),
        )
    finally:
        try:
            script.unlink(missing_ok=True)
        except OSError:
            pass

    return {
        "returncode": proc_code,
        "stdout": truncate_output(out),
        "stderr": truncate_output(err),
    }