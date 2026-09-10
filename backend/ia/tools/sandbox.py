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
    AGENT_PRIVATE_HOST_ALLOWLIST  CSV d'hôtes privés exemptés du blocage
                              précédent (ex. une instance SearXNG locale pour
                              web_search : 127.0.0.1,localhost,searxng).
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
        raise ValueError('run_command attend une LISTE non vide, ex: ["git", "--version"].')
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
# P0 SEC (F8) : fail-closed — blocage des hôtes privés/boucle locale ACTIF PAR
# DÉFAUT. Désactivation EXPLICITE uniquement via AGENT_BLOCK_PRIVATE_HOSTS=0 /
# false / no / off (ex. dev local sans SearXNG). En prod compose, le service
# « searxng » reste joignable via AGENT_PRIVATE_HOST_ALLOWLIST (défaut :
# searxng,127.0.0.1,localhost — cf. docker-compose.yml).
_SSRF_OFF_VALUES = frozenset({"0", "false", "no", "off"})
# Borne anti-OOM : aucun corps HTTP téléchargé au-delà (stream + tronqué).
MAX_DOWNLOAD_BYTES = 2_000_000


def ssrf_protection_enabled() -> bool:
    """True sauf désactivation explicite (fail-closed P0 : défaut ON)."""
    return os.getenv("AGENT_BLOCK_PRIVATE_HOSTS", "").strip().lower() not in _SSRF_OFF_VALUES


def url_scheme_allowed(url: str) -> None:
    """Accepte uniquement http/https avec un hôte."""
    parsed = urlparse(str(url))
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Schéma interdit '{parsed.scheme or '?'}' : http/https uniquement.")
    if not parsed.netloc:
        raise ValueError(f"URL invalide (hôte manquant) : {url}")


def host_is_private(hostname: str) -> bool:
    """True si `hostname` résout vers une adresse privée/boucle locale.

    P0 SEC (F8) : un hôte IRRESOLVABLE (DNS menteur, rebond, fake de test
    sans réseau) n'est PAS traité comme privé — la résolution réelle reste
    l'arbitre (sinon tout domaine public sans DNS local serait bloqué, y
    compris les fakes offline des tests). Les IP littérales privées
    (192.168.x, 10.x, 127.x, ::1…) restent interdites sans allowlist.
    """
    import ipaddress
    import socket

    name = (hostname or "").strip().lower().rstrip(".")
    # IP littérale : décision purement locale, sans DNS (fiable + offline).
    try:
        ip = ipaddress.ip_address(name)
        return bool(
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
        )
    except ValueError:
        pass  # nom DNS : résolution réelle ci-dessous
    try:
        infos = socket.getaddrinfo(name, None)
    except OSError:
        return False  # irresolvable -> PAS de blocage (résolution = arbitre)
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


def get_private_host_allowlist() -> set[str]:
    """Hôtes privés explicitement autorisés (AGENT_PRIVATE_HOST_ALLOWLIST, CSV).

    Exemple : AGENT_PRIVATE_HOST_ALLOWLIST=127.0.0.1,localhost,searxng
    """
    raw = os.getenv("AGENT_PRIVATE_HOST_ALLOWLIST", "")
    return {entry.strip().lower() for entry in raw.split(",") if entry.strip()}


def enforce_host_policy(url: str) -> None:
    """Applique la politique SSRF si la protection est activée (défaut ON, P0).

    Les hôtes listés dans AGENT_PRIVATE_HOST_ALLOWLIST (CSV) sont exemptés :
    utile pour joindre un service local de confiance, ex. une instance
    SearXNG utilisée par web_search (ia/tools/web_tools.py).
    Désactivation explicite : AGENT_BLOCK_PRIVATE_HOSTS=0/false/no/off.
    """
    if not ssrf_protection_enabled():
        return
    hostname = (urlparse(str(url)).hostname or "").lower()
    if not hostname or hostname in get_private_host_allowlist():
        return
    if host_is_private(hostname):
        raise PermissionError(f"Hôte privé/loopback interdit (protection SSRF active) : {hostname}")


def enforce_response_host_policy(url: str) -> None:
    """Re-valide l'hôte FINAL après redirect (anti-bypass 302 → 169.254…)."""
    enforce_host_policy(url)


def iso_from_timestamp(ts: float) -> str:
    """Timestamp -> chaîne ISO lisible (pour les listings de fichiers)."""
    return datetime.fromtimestamp(ts).isoformat(sep=" ", timespec="seconds")
