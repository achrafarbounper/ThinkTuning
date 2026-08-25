"""Outils Docker de l'agent : ps, logs, exec — via le CLI `docker` en sous-processus.

Sans shell intermédiaire (shell=False) : pas d'injection. Timeout et sorties
plafonnées gérés par `tools.sandbox.run_subprocess`.
"""

import json

from tools.sandbox import run_subprocess

DOCKER_TIMEOUT_S = 30.0
_PS_MAX_CHARS = 16000  # une ligne JSON par conteneur


def _ensure_docker_output(returncode: int, stdout: str, stderr: str, context: str) -> str:
    if returncode != 0:
        detail = stderr.strip() or stdout.strip() or "erreur inconnue"
        raise RuntimeError(f"docker {context} a échoué (code {returncode}) : {detail}")
    return stdout


# --- ps ---------------------------------------------------------------------------
def docker_ps(all_containers: bool = False) -> list[dict]:
    """Liste les conteneurs (un objet JSON par conteneur, format `docker ps`)."""
    argv = ["docker", "ps"]
    if all_containers:
        argv.append("--all")
    argv += ["--format", "{{json .}}"]

    code, out, err = run_subprocess(
        argv, timeout=DOCKER_TIMEOUT_S, max_output_chars=_PS_MAX_CHARS
    )
    out = _ensure_docker_output(code, out, err, "ps")

    containers = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            containers.append(json.loads(line))
        except json.JSONDecodeError:
            containers.append({"raw": line})  # ligne partielle (troncature) -> conservée brute
    return containers


# --- logs ---------------------------------------------------------------------------
def docker_logs(container: str, tail: int = 100, timestamps: bool = False) -> str:
    """Dernières `tail` lignes de logs d'un conteneur (stdout + stderr)."""
    tail = max(1, min(int(tail), 5000))
    argv = ["docker", "logs", "--tail", str(tail)]
    if timestamps:
        argv.append("--timestamps")
    argv.append(str(container))

    code, out, err = run_subprocess(argv, timeout=DOCKER_TIMEOUT_S)
    out = _ensure_docker_output(code, out, err, f"logs {container}")
    combined = out if not err.strip() else f"{out}\n[stderr]\n{err}"
    return combined.strip()


# --- exec -----------------------------------------------------------------------------
def docker_exec(container: str, command: str, workdir: str | None = None,
                user: str | None = None) -> dict:
    """Exécute `command` (chaîne, via `sh -c`) dans le conteneur.

    Prérequis : le conteneur expose un shell POSIX (/bin/sh) — vrai pour la
    quasi-totalité des images Linux.
    """
    if not str(command).strip():
        raise ValueError("'command' ne peut pas être vide.")
    argv = ["docker", "exec"]
    if workdir:
        argv += ["--workdir", str(workdir)]
    if user:
        argv += ["--user", str(user)]
    argv += [str(container), "sh", "-c", str(command)]

    code, out, err = run_subprocess(argv, timeout=DOCKER_TIMEOUT_S)
    return {"container": container, "returncode": code, "stdout": out, "stderr": err}