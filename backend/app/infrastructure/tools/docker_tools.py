"""Outils Docker de l'agent : ps, logs, exec — via le CLI `docker` en sous-processus.

Sans shell intermédiaire (shell=False) : pas d'injection. Timeout et sorties
plafonnées gérés par `tools.sandbox.run_subprocess`.

P1 SEC (confinement, point 8) :
    - `docker_exec` exige une LISTE d'arguments (une chaîne est rejetée) :
      plus JAMAIS de `sh -c` implicite → plus d'injection déléguée ni de
      contournement de l'allowlist hôte par la chaîne ;
    - `docker_exec` est limité à une allowlist de conteneurs
      (AGENT_DOCKER_ALLOWED_CONTAINERS, défaut conservateur `thinktuning-app`)
      — fail-closed si non configuré.
"""

import json
import os

from .sandbox import run_subprocess

DOCKER_TIMEOUT_S = 30.0
_PS_MAX_CHARS = 16000  # une ligne JSON par conteneur

# Défaut conservateur : le conteneur principal du compose (service « app »).
# Les opérateurs peuvent étendre via AGENT_DOCKER_ALLOWED_CONTAINERS (CSV).
DEFAULT_ALLOWED_CONTAINERS = "thinktuning-app"


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
def _allowed_docker_containers() -> set[str]:
    """CSV des conteneurs autorisés pour docker_exec (relu à chaque appel)."""
    raw = os.getenv("AGENT_DOCKER_ALLOWED_CONTAINERS", DEFAULT_ALLOWED_CONTAINERS)
    return {entry.strip().lower() for entry in raw.split(",") if entry.strip()}


def _docker_inventory() -> dict[str, str]:
    """Inventaire {nom: ID} des conteneurs COURANTS, via `docker ps --format json`.

    Retourne un dict vide si le daemon est injoignable : l'allowlist CSV reste
    alors l'arbitre unique (jamais de déblocage par défaut sur erreur).
    """
    inventory: dict[str, str] = {}
    try:
        code, out, err = run_subprocess(
            ["docker", "ps", "--format", "{{json .}}"],
            timeout=DOCKER_TIMEOUT_S,
            max_output_chars=_PS_MAX_CHARS,
        )
    except RuntimeError:
        return inventory
    if code != 0:
        return inventory
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = str(data.get("Names") or data.get("name") or "").strip()
        cid = str(data.get("ID") or data.get("id") or "").strip()
        if name:
            inventory.setdefault(name, cid)
    return inventory


def _ensure_container_allowed(container: str) -> None:
    """Verdict físico de l'allowlist conteneurs (fail-closed, P1 point 8).

    - correspondance exacte (insensible à la casse) contre le CSV
      AGENT_DOCKER_ALLOWED_CONTAINERS ;
    - sinon, contre l'inventaire RÉEL du daemon (nom court ou ID complet) ;
    - CSV vide / non défini -> refus systématique.
    """
    target = str(container).strip().lower()
    allowed = _allowed_docker_containers()
    if target in allowed:
        return
    if not allowed:
        raise PermissionError(
            "AGENT_DOCKER_ALLOWED_CONTAINERS absent/vide : docker_exec refusé "
            "(fail-closed — définissez la liste CSV des conteneurs autorisés)."
        )
    for name, cid in _docker_inventory().items():
        if target in (name.lower(), cid.lower()):
            return
    raise PermissionError(
        f"Conteneur docker_exec refusé : '{container}' (hors allowlist "
        f"AGENT_DOCKER_ALLOWED_CONTAINERS={','.join(sorted(allowed))})."
    )


def docker_exec(container: str, command: list, workdir: str | None = None,
                user: str | None = None) -> dict:
    """Exécute `command` (LISTE d'arguments, sans shell) dans le conteneur.

    P1 SEC (confinement) : ``command`` DOIT être une liste — une chaîne est
    rejetée (``ValueError``) : plus jamais de ``sh -c`` implicite, donc plus
    d'injection déléguée ni de contournement de l'allowlist hôte. ``container``
    doit figurer dans ``AGENT_DOCKER_ALLOWED_CONTAINERS`` (défaut :
    ``thinktuning-app``) ; la résolution nom/ID via ``docker ps`` est acceptée
    en second arbitre. Verdict de policy : APPROVE (validation humaine, cf.
    ia/agent/approvals.py).
    """
    if not isinstance(command, (list, tuple)) or not command:
        raise ValueError(
            "'command' doit être une LISTE d'arguments (ex: [\"ls\", \"-la\"]), "
            "jamais une chaîne — l'exécution via `sh -c` est bloquée (P1)."
        )
    if not str(container).strip():
        raise ValueError("'container' ne peut pas être vide.")
    _ensure_container_allowed(container)

    argv = ["docker", "exec"]
    if workdir:
        argv += ["--workdir", str(workdir)]
    if user:
        argv += ["--user", str(user)]
    argv += [str(container)] + [str(c) for c in command]

    code, out, err = run_subprocess(argv, timeout=DOCKER_TIMEOUT_S)
    return {
        "container": container,
        "command": [str(c) for c in command],
        "returncode": code,
        "stdout": out,
        "stderr": err,
    }