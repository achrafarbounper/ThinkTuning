"""Outils système de l'agent : ls, find, cat, mkdir, cp, mv, rm — tous sandboxés.

Tous les chemins passent par `tools.sandbox.safe_resolve` : impossible de
lire/écrire/supprimer en dehors de la racine autorisée (AGENT_SANDBOX_ROOT,
défaut : répertoire courant du process).
"""

import re
import shutil
from pathlib import Path

from tools.sandbox import iso_from_timestamp, safe_resolve

MAX_LIST_ENTRIES = 500
DEFAULT_READ_BYTES = 65536  # 64 Ko
MAX_FIND_RESULTS = 100


# --- ls ---------------------------------------------------------------------------
def list_dir(path: str = ".", recursive: bool = False) -> dict:
    """Liste un répertoire (dossiers d'abord, puis fichiers, ordre alphabétique)."""
    target = safe_resolve(path, must_exist=True)
    if not target.is_dir():
        raise NotADirectoryError(f"Pas un répertoire : {target}")

    root = get_root()
    entries: list[dict] = []
    iterator = target.rglob("*") if recursive else target.iterdir()
    for item in iterator:
        try:
            stat = item.stat()
            kind = "dir" if item.is_dir() else ("file" if item.is_file() else "other")
            entries.append(
                {
                    "path": str(item.relative_to(root)) if recursive else item.name,
                    "type": kind,
                    "size_bytes": stat.st_size if kind == "file" else None,
                    "modified": iso_from_timestamp(stat.st_mtime),
                }
            )
        except OSError:
            continue  # entrée illisible -> ignorée silencieusement
        if len(entries) >= MAX_LIST_ENTRIES:
            break

    entries.sort(key=lambda e: (e["type"] != "dir", e["path"].lower()))
    return {
        "path": str(target),
        "entry_count": len(entries),
        "truncated": len(entries) >= MAX_LIST_ENTRIES,
        "entries": entries,
    }


# --- cat ---------------------------------------------------------------------------
def read_file(path: str, max_bytes: int = DEFAULT_READ_BYTES) -> str:
    """Lit un fichier texte (UTF-8) ; tronque au-delà de max_bytes."""
    try:
        file_path = safe_resolve(path, must_exist=True)
    except FileNotFoundError as exc:
        # Guide l'agent vers la bonne stratégie au lieu de le laisser deviner :
        # ce message remonte tel quel au LLM via AgentCore.
        raise FileNotFoundError(
            f"{exc} — utilisez le tool 'find_file' pour localiser le vrai chemin "
            '(ex: {"tool": "find_file", "args": {"pattern": "default"}}).'
        ) from exc
    if not file_path.is_file():
        raise IsADirectoryError(f"Pas un fichier : {file_path}")

    max_bytes = max(1, int(max_bytes))
    with open(file_path, "rb") as handle:
        data = handle.read(max_bytes)
    text = data.decode("utf-8", errors="replace")

    real_size = file_path.stat().st_size
    if real_size > max_bytes:
        text += f"\n… [tronqué : {real_size} octets au total, {max_bytes} affichés]"
    return text


# --- find --------------------------------------------------------------------------
def find_file(pattern: str, path: str = ".", max_results: int = MAX_FIND_RESULTS) -> dict:
    """Cherche récursivement les fichiers/dossiers dont le chemin relatif ou le
    nom correspond à `pattern` (regex Python, insensible à la casse).

    Retourne des chemins relatifs à la racine sandbox, directement exploitables
    par read_file / copy_path / move_path. À utiliser quand read_file répond
    « Introuvable », au lieu de deviner des chemins.
    """
    try:
        regex = re.compile(str(pattern), re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"Regex invalide '{pattern}' : {exc}") from exc

    max_results = max(1, int(max_results))
    base = safe_resolve(path, must_exist=True)
    if not base.is_dir():
        raise NotADirectoryError(f"Pas un répertoire : {base}")

    root = get_root()
    matches: list[dict] = []
    for item in base.rglob("*"):
        relative = item.relative_to(root).as_posix()
        # Match sur le chemin complet OU uniquement le nom : un pattern ancré
        # (^default\.yaml$) retrouve ainsi le fichier quelle que soit sa profondeur.
        if regex.search(relative) or regex.search(item.name):
            matches.append(
                {
                    "path": relative,
                    "type": "dir" if item.is_dir() else ("file" if item.is_file() else "other"),
                }
            )
            if len(matches) >= max_results:
                break

    matches.sort(key=lambda m: m["path"].lower())
    return {
        "pattern": str(pattern),
        "search_path": str(base),
        "match_count": len(matches),
        "truncated": len(matches) >= max_results,
        "matches": matches,
    }


# --- mkdir -------------------------------------------------------------------------
def make_dir(path: str) -> str:
    """Crée un répertoire (parents inclus, sans erreur s'il existe déjà)."""
    created = safe_resolve(path)
    created.mkdir(parents=True, exist_ok=True)
    return f"Répertoire prêt : {created}"


# --- cp -----------------------------------------------------------------------------
def copy_path(src: str, dst: str) -> str:
    """Copie fichier ou arborescence dans la sandbox."""
    source = safe_resolve(src, must_exist=True)
    destination = safe_resolve(dst)

    if source.is_dir():
        if destination.exists():
            raise FileExistsError(f"Destination existante : {destination} (copie d'arborescence refusée).")
        shutil.copytree(source, destination)
    else:
        final = destination / source.name if destination.is_dir() else destination
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, final)
        destination = final
    return f"Copié : {source} -> {destination}"


# --- mv -------------------------------------------------------------------------------
def move_path(src: str, dst: str) -> str:
    """Déplace/renomme fichier ou répertoire dans la sandbox."""
    source = safe_resolve(src, must_exist=True)
    destination = safe_resolve(dst)
    if source == destination:
        return f"Source et destination identiques, rien à faire : {source}"
    moved = shutil.move(str(source), str(destination))
    return f"Déplacé : {source} -> {moved}"


# --- rm ---------------------------------------------------------------------------------
def remove_path(path: str, recursive: bool = False) -> str:
    """Supprime fichier ou répertoire.

    Garde-fous :
        - la racine de la sandbox elle-même n'est jamais supprimable ;
        - tout ce qui est sous `.git` est interdit ;
        - un répertoire non vide exige recursive=true.
    """
    target = safe_resolve(path, must_exist=True)

    root = get_root()
    if target == root:
        raise PermissionError("Suppression de la racine de la sandbox interdite.")
    relative = target.relative_to(root)
    if ".git" in relative.parts:
        raise PermissionError("Suppression sous '.git' interdite.")

    if target.is_dir():
        non_empty = any(target.iterdir())
        if non_empty and not recursive:
            raise ValueError(
                f"Répertoire non vide : {target}. Relancez avec recursive=true."
            )
        shutil.rmtree(target) if non_empty else target.rmdir()
    else:
        target.unlink()
    return f"Supprimé : {target}"


def get_root() -> Path:
    """Racine courante de la sandbox (ré-export pratique pour les tests)."""
    from tools.sandbox import get_sandbox_root

    return get_sandbox_root()