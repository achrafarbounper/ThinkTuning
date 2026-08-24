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

import os
import sys
import uuid
from pathlib import Path

from tools.sandbox import (
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

    Le script vit sous `<sandbox>/.agent_tmp/` et est supprimé après exécution.
    stdout/stderr capturés et plafonnés.
    """
    if not isinstance(code, str) or not code.strip():
        raise ValueError("'code' doit être une chaîne Python non vide.")

    tmp_dir = get_sandbox_root() / ".agent_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    script = tmp_dir / f"snippet_{uuid.uuid4().hex}.py"
    script.write_text(code, encoding="utf-8")

    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    try:
        proc_code, out, err = run_subprocess(
            [sys.executable, str(script)],
            timeout=max(1.0, min(float(timeout), 300.0)),
            cwd=get_sandbox_root(),
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