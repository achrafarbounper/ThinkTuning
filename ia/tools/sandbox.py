"""Bac à sable (sandbox) pour les outils de l'agent IA.

Toutes les opérations sur fichiers passent par `safe_resolve` : un chemin ne
peut jamais sortir du répertoire racine autorisé.

Configuration (variables d'environnement, relues à chaque appel) :
    AGENT_SANDBOX_ROOT        racine autorisée (défaut : répertoire courant du
                              process, donc la racine du projet si uvicorn /
                              pytest sont lancés depuis celle-ci).
    AGENT_ALLOWED_BINARIES    allowlist CSV des exécutables autorisés par
                              run_command (voir DEFAULT_ALLOWED_BINARIES).
    AGENT_BLOCK_PRIVATE_HOSTS "1"/"true" pour interdire à http_get/http_post
                              de joindre des hôtes privés/loopback (anti-SSRF).
"""

import os
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

# --- Constantes -----------------------------------------------------------------
DEFAULT_MAX_OUTPUT_CHARS = 8000
DEFAULT_TIMEOUT_SECONDS = 30

# Shells volontairement ABSENTS de l'allowlist : ils permettraient d'exécuter
# n'importe quoi et annuleraient le filtrage (cmd, powershell, bash, sh, ...).
DEFAULT_ALLOWED_BINARIES = (
    "python,python3,pip,pytest,git,docker,nvidia-smi,"
    "node,npm,curl,wget,"
    "ls,dir,cat,type,head,tail,grep,findstr,find,echo,wc,diff,sort,uniq,tree,"
    "whoami,hostname,tasklist"
)


# --- Racine de la sandbox ---------------------------------------------------------
def get_sandbox_root() -> Path:
    """Racine autorisée pour les opérations fichiers (relue à chaque appel)."""
    root = os.getenv("AGENT_SANDBOX_ROOT")
    if root:
        return Path(root).expanduser().resolve()
    return Path.cwd().resolve()


def safe_resolve(path: str | Path, must_exist: bool = False) -> Path:
    """Résout `path` dans la sandbox et refuse toute évasion.

    - Chemins relatifs : résolus depuis la racine de la sandbox.
    - Chemins absolus : acceptés UNIQUEMENT s'ils restent sous la racine.
    - `..` et liens qui feraient sortir de la racine : PermissionError.
    """
    root = get_sandbox_root()
    candidate = Path(str(path)).expanduser()
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise PermissionError(
            f"Chemin hors sandbox interdit : '{path}' (racine autorisée : {root})"
        )
    if must_exist and not resolved.exists():
        raise FileNotFoundError(f"Introuvable : {resolved}")
    return resolved


def truncate_output(text: str, limit: int = DEFAULT_MAX_OUTPUT_CHARS) -> str:
    """Plafonne la taille des sorties pour ne pas saturer le contexte du LLM."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [tronqué, {len(text)} caractères au total]"


# --- Sous-processus ----------------------------------------------------------------
def run_subprocess(
    argv: list,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    cwd: Path | None = None,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> tuple[int, str, str]:
    """Exécute `argv` SANS shell, avec timeout et sorties plafonnées.

    Retourne (returncode, stdout, stderr). Lève RuntimeError sur exécutable
    introuvable ou timeout — messages propres exploitables par l'agent.
    """
    timeout = max(1.0, min(float(timeout), 600.0))
    try:
        proc = subprocess.run(
            [str(a) for a in argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            shell=False,  # jamais de shell -> pas d'injection
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Exécutable introuvable : {argv[0]} ({exc})") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Timeout : la commande '{' '.join(map(str, argv))}' a dépassé {timeout:g}s."
        ) from exc
    return (
        proc.returncode,
        truncate_output(proc.stdout or "", max_output_chars),
        truncate_output(proc.stderr or "", max_output_chars),
    )


# --- Allowlist de binaires -----------------------------------------------------------
def get_allowed_binaries() -> set[str]:
    raw = os.getenv("AGENT_ALLOWED_BINARIES", DEFAULT_ALLOWED_BINARIES)
    return {entry.strip().lower() for entry in raw.split(",") if entry.strip()}


def check_command_allowed(command: list) -> str:
    """Vérifie que l'exécutable (argv[0]) est dans l'allowlist. Retourne son nom."""
    if not isinstance(command, (list, tuple)) or not command:
        raise ValueError(
            "run_command attend une LISTE non vide, ex: [\"git\", \"--version\"]."
        )
    exe = Path(str(command[0])).name.lower()
    if exe.endswith(".exe"):
        exe = exe[: -len(".exe")]
    allowed = get_allowed_binaries()
    if exe not in allowed:
        preview = ", ".join(sorted(allowed)[:12])
        raise PermissionError(
            f"Binaire interdit : '{exe}'. Autorisés (extrait) : {preview}, … "
            "(configurables via AGENT_ALLOWED_BINARIES)."
        )
    return exe


# --- Garde SSRF pour les outils réseau --------------------------------------------------
def url_scheme_allowed(url: str) -> None:
    """Accepte uniquement http/https avec un hôte."""
    parsed = urlparse(str(url))
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Schéma interdit '{parsed.scheme or '?'}' : http/https uniquement.")
    if not parsed.netloc:
        raise ValueError(f"URL invalide (hôte manquant) : {url}")


def host_is_private(hostname: str) -> bool:
    """True si `hostname` résout vers une adresse privée/boucle locale."""
    import ipaddress
    import socket

    try:
        infos = socket.getaddrinfo(hostname, None)
    except OSError:
        return True  # hôte irresolvable -> traité comme interdit
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        ):
            return True
    return False


def enforce_host_policy(url: str) -> None:
    """Applique la politique SSRF si AGENT_BLOCK_PRIVATE_HOSTS est activée."""
    if os.getenv("AGENT_BLOCK_PRIVATE_HOSTS", "").strip().lower() in ("1", "true", "yes"):
        hostname = urlparse(str(url)).hostname or ""
        if hostname and host_is_private(hostname):
            raise PermissionError(
                f"Hôte privé/loopback interdit (AGENT_BLOCK_PRIVATE_HOSTS actif) : {hostname}"
            )


def iso_from_timestamp(ts: float) -> str:
    """Timestamp -> chaîne ISO lisible (pour les listings de fichiers)."""
    return datetime.fromtimestamp(ts).isoformat(sep=" ", timespec="seconds")