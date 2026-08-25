"""Outils d'exploitation pour l'agent IA : téléchargement, environnement,
disque, archives zip, git et docker stats.

Sécurité :
    - download_file : schémas http(s) + politique anti-SSRF réutilisées depuis
      tools.sandbox, écriture confinée à la sandbox, taille maximale stricte
      (fichier partiel supprimé en cas de dépassement) ;
    - git_* : argv-list SANS shell via sandbox.run_subprocess, dépôt épinglé à
      la racine de la sandbox, --no-pager obligatoire ; le code retour est
      renvoyé tel quel (pas d'exception) pour que l'agent raisonne dessus ;
    - unzip_file : protection zip-slip ('..', chemins absolus et lettres de
      lecteur refusés), extraction confinée au dossier cible de la sandbox.
"""

import os
import platform
import json
import shutil
import sys
import zipfile
from importlib import metadata
from pathlib import PurePosixPath

import requests

from .docker_tools import _ensure_docker_output
from .sandbox import (
    enforce_host_policy,
    run_subprocess,
    safe_resolve,
    truncate_output,
    url_scheme_allowed,
)
from .search_tools import SKIP_DIRS

DOWNLOAD_CHUNK_BYTES = 64 * 1024
DEFAULT_DOWNLOAD_TIMEOUT_S = 60.0
MAX_DOWNLOAD_MB = 500
GIT_MAX_OUTPUT_CHARS = 16000
DOCKER_TIMEOUT_S = 30.0

# Versions de packages utiles au diagnostic (jamais de variables d'environnement :
# elles peuvent contenir des secrets).
_REPORTED_PACKAGES = (
    "torch", "transformers", "datasets", "scikit-learn",
    "pandas", "numpy", "fastapi", "requests", "psycopg2-binary",
)


# --- téléchargement ------------------------------------------------------------------
def download_file(url: str, filename: str, max_mb: int = 50,
                  timeout: float = DEFAULT_DOWNLOAD_TIMEOUT_S) -> dict:
    """Télécharge un fichier http(s) DANS la sandbox, en streaming.

    - politique schéma/hôte identique à http_get (AGENT_BLOCK_PRIVATE_HOSTS) ;
    - stoppe et SUPPRIME le fichier partiel au-delà de max_mb (plafond 500 Mo) ;
    - échoue proprement sur les codes HTTP >= 400 (pas de page d'erreur sauvée).
    """
    url_scheme_allowed(url)
    enforce_host_policy(url)
    max_bytes = int(float(max_mb) * 1024 * 1024)
    if not 0 < max_bytes <= MAX_DOWNLOAD_MB * 1024 * 1024:
        raise ValueError(f"'max_mb' doit être entre 0 et {MAX_DOWNLOAD_MB}.")
    timeout = max(1.0, min(float(timeout), 300.0))

    target = safe_resolve(filename)
    target.parent.mkdir(parents=True, exist_ok=True)

    response = requests.get(url, stream=True, timeout=timeout)
    if response.status_code >= 400:
        response.close()
        raise RuntimeError(
            f"Téléchargement impossible : HTTP {response.status_code} pour {url}"
        )

    written = 0
    try:
        with open(target, "wb") as handle:
            for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_BYTES):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise ValueError(
                        f"Fichier trop volumineux (> {max_mb} Mo) : téléchargement annulé."
                    )
                handle.write(chunk)
    except Exception:
        target.unlink(missing_ok=True)  # jamais de fichier partiel traînant
        raise
    finally:
        response.close()

    return {
        "path": str(target),
        "bytes_written": written,
        "url": url,
        "status": response.status_code,
    }


# --- environnement ---------------------------------------------------------------------
def env_info() -> dict:
    """Diagnostic lecture seule : Python, OS, CPU et versions des packages clés."""
    versions: dict[str, str | None] = {}
    for name in _REPORTED_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "executable": sys.executable,
        "cpu_count": os.cpu_count(),
        "packages": versions,
        "note": "Les variables d'environnement ne sont jamais exposées (risque de secrets).",
    }


# --- disque -----------------------------------------------------------------------------
_MAX_WALK_FILES_PER_CHILD = 50_000
_MAX_LISTED_CHILDREN = 30


def disk_usage(path: str = ".") -> dict:
    """Espace disque libre + taille des enfants directs d'un dossier sandbox
    (les entraînements meurent silencieusement sur un disque plein)."""
    target = safe_resolve(path, must_exist=True)
    total, used, free = shutil.disk_usage(target)

    children: list[dict] = []
    listed_truncated = False
    if target.is_dir():
        for child in sorted(target.iterdir()):
            size_bytes = 0
            walked = 0
            if child.is_file():
                size_bytes = child.stat().st_size
            else:
                for sub_item in child.rglob("*"):
                    try:
                        if sub_item.is_file():
                            size_bytes += sub_item.stat().st_size
                        walked += 1
                        if walked >= _MAX_WALK_FILES_PER_CHILD:
                            break
                    except OSError:
                        continue
            children.append(
                {
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                    "size_bytes": size_bytes,
                }
            )
        children.sort(key=lambda entry: entry["size_bytes"], reverse=True)
        if len(children) > _MAX_LISTED_CHILDREN:
            children = children[:_MAX_LISTED_CHILDREN]
            listed_truncated = True

    return {
        "path": str(target),
        "total_gb": round(total / 1024**3, 2),
        "used_gb": round(used / 1024**3, 2),
        "free_gb": round(free / 1024**3, 2),
        "children": children,
        "children_truncated": listed_truncated,
    }


# --- archives ---------------------------------------------------------------------------
_MAX_ZIP_ENTRIES = 10_000


def zip_path(src: str, dst: str) -> dict:
    """Compresse un fichier ou un dossier de la sandbox vers une archive .zip.

    - `.git`, venv, node_modules et caches sont exclus ;
    - les chemins stockés dans l'archive sont relatifs à la racine sandbox
      (extraction stable et prévisible par unzip_file).
    """
    source = safe_resolve(src, must_exist=True)
    destination = safe_resolve(dst)
    if destination.exists():
        raise FileExistsError(f"Destination existante : {destination}")
    if destination.suffix.lower() != ".zip":
        raise ValueError(f"L'archive doit finir par .zip (reçu : '{destination.name}').")

    files = (
        [source] if source.is_file()
        else [item for item in sorted(source.rglob("*"))
              if item.is_file() and not (SKIP_DIRS & {p.name for p in item.parents})]
    )
    if len(files) > _MAX_ZIP_ENTRIES:
        raise ValueError(f"Trop de fichiers à compresser (> {_MAX_ZIP_ENTRIES}).")
    if not files:
        raise ValueError("Rien à compresser.")

    root = safe_resolve(".")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for item in files:
            archive.write(item, arcname=item.relative_to(root).as_posix())
    return {
        "archive": str(destination),
        "file_count": len(files),
        "size_bytes": destination.stat().st_size,
    }


def unzip_file(src: str, dst: str) -> dict:
    """Extrait une archive .zip de la sandbox vers un dossier de la sandbox.

    Protection zip-slip : chaque entrée est validée (pas de chemin absolu, pas
    de '..', pas de lettre de lecteur) puis re-résolue sous la destination.
    """
    archive_path = safe_resolve(src, must_exist=True)
    if not zipfile.is_zipfile(archive_path):
        raise ValueError(f"Pas une archive zip valide : {archive_path}")
    destination = safe_resolve(dst)
    destination.mkdir(parents=True, exist_ok=True)
    resolved_destination = destination.resolve()

    extracted_files = 0
    created_dirs = 0
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.namelist():
            pure = PurePosixPath(member)
            if pure.is_absolute() or ".." in pure.parts or ":" in member:
                raise PermissionError(
                    f"Entrée dangereuse refusée dans l'archive : '{member}'"
                )
            target = resolved_destination.joinpath(*pure.parts)
            if resolved_destination != target and resolved_destination not in target.parents:
                raise PermissionError(
                    f"Extraction hors du dossier cible refusée : '{member}'"
                )
            if member.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                created_dirs += 1
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as reader, open(target, "wb") as writer:
                shutil.copyfileobj(reader, writer)
            extracted_files += 1
    return {
        "destination": str(destination),
        "extracted_files": extracted_files,
        "created_dirs": created_dirs,
    }


# --- git --------------------------------------------------------------------------------
def _run_git(argv: list[str]) -> dict:
    """Exécute git SANS shell depuis la racine sandbox ; renvoie le résultat brut.

    Contrairement à docker_tools, PAS d'exception sur code != 0 : l'agent lit
    lui-même returncode/stderr (ex : 'not a git repository' = réponse utile).
    """
    root = safe_resolve(".")
    code, out, err = run_subprocess(
        argv, cwd=root, timeout=30.0, max_output_chars=GIT_MAX_OUTPUT_CHARS
    )
    return {"command": argv, "returncode": code, "stdout": out, "stderr": err}


def git_status() -> dict:
    """`git status --short --branch` sur le dépôt de la racine sandbox."""
    return _run_git(["git", "--no-pager", "status", "--short", "--branch"])


def git_log(limit: int = 20) -> dict:
    """`git log --oneline` des N derniers commits (limit plafonné à 100)."""
    limit = max(1, min(int(limit), 100))
    return _run_git(["git", "--no-pager", "log", "--oneline", "-n", str(limit)])


def git_diff(path: str | None = None, staged: bool = False) -> dict:
    """`git diff` (index <-> travail), optionnellement `--cached` et restreint
    à un chemin de la sandbox."""
    argv = ["git", "--no-pager", "diff"]
    if staged:
        argv.append("--cached")
    if path:
        target = safe_resolve(path, must_exist=True)
        argv.append(target.relative_to(safe_resolve(".")).as_posix())
    return _run_git(argv)


# --- docker stats --------------------------------------------------------------------------
def docker_stats(all_containers: bool = False) -> list[dict]:
    """Consommation CPU/RAM par conteneur (`docker stats --no-stream`,
    un objet JSON par conteneur — même convention que docker_ps)."""
    argv = ["docker", "stats", "--no-stream"]
    if all_containers:
        argv.append("--all")
    argv += ["--format", "{{json .}}"]

    code, out, err = run_subprocess(
        argv, timeout=DOCKER_TIMEOUT_S, max_output_chars=16000
    )
    out = _ensure_docker_output(code, out, err, "stats")

    containers: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            containers.append(json.loads(line))
        except json.JSONDecodeError:
            containers.append({"raw": line})  # ligne partielle (troncature)
    return containers