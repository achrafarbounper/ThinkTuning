"""Outils de recherche et lecture ciblée de fichiers pour l'agent IA.

Complète system_tools (ls/cat/find sur les NOMS) avec :
    - search_in_files : recherche regex dans le CONTENU des fichiers (grep) ;
    - tail_file       : dernières lignes d'un fichier (logs d'entraînement…) ;
    - append_file     : ajout en fin de fichier sans réécriture complète ;
    - now             : horodatage ISO pour corréler jobs / logs.

Sécurité : tous les chemins passent par `sandbox.safe_resolve`, les sorties
sont plafonnées, et la recherche ignore .git / venv / node_modules / caches.
"""

import re
from datetime import datetime, timezone

from .sandbox import safe_resolve, truncate_output

MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024  # 2 Mo : au-delà, fichier ignoré
DEFAULT_LINE_PREVIEW_CHARS = 200
MAX_TAIL_BUDGET_BYTES = 256 * 1024       # budget de lecture arrière pour tail
TAIL_CHUNK_BYTES = 4096

# Dossiers systématiquement exclus du balayage (volumineux / hors sujet).
SKIP_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".agent_tmp",
    "venv", ".venv", "env", "node_modules",
}


def _looks_binary(chunk: bytes) -> bool:
    """Heuristique simple : un octet NUL dans le premier Ko => binaire."""
    return b"\x00" in chunk[:1024]


# --- grep ---------------------------------------------------------------------------
def search_in_files(pattern: str, path: str = ".", glob: str = "*",
                    max_results: int = 100) -> dict:
    """Cherche `pattern` (regex Python, insensible à la casse) dans le contenu
    des fichiers sous `path`.

    - `glob` filtre les noms de fichiers (ex : "*.py", "*.log") ;
    - les fichiers > 2 Mo et les binaires sont ignorés ;
    - `.git`, `venv`, `node_modules`, caches… ne sont jamais balayés.

    Retourne des correspondances {path, line_number, text} avec des chemins
    relatifs à la racine sandbox, exploitables directement par read_file.
    """
    try:
        regex = re.compile(str(pattern), re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"Regex invalide '{pattern}' : {exc}") from exc

    base = safe_resolve(path, must_exist=True)
    root = safe_resolve(".")

    def _excluded(item) -> bool:
        """True si l'entrée vit dans un dossier exclu SOUS la racine de recherche.

        Les exclusions (.git, venv, caches…) ne s'appliquent qu'aux segments de
        chemin PARCOURUS : une cible explicitement passée en `path` (même un
        fichier isolé) est toujours honorée.
        """
        if not base.is_dir() or item == base:
            return False
        try:
            segments = set(item.relative_to(base).parts[:-1])
        except ValueError:  # hors de la racine de recherche (scan fichier unique)
            return False
        return bool(SKIP_DIRS & segments)

    matches: list[dict] = []
    scanned_files = 0
    skipped_oversized = 0
    truncated = False
    max_results = max(1, int(max_results))

    def _iter_candidates():
        if base.is_dir():
            yield from base.rglob(glob if glob else "*")
        else:
            yield base

    for item in _iter_candidates():
        if len(matches) >= max_results:
            truncated = True
            break
        try:
            if _excluded(item) or not item.is_file():
                continue
            if item.stat().st_size > MAX_SEARCH_FILE_BYTES:
                skipped_oversized += 1
                continue
            with open(item, "rb") as handle:
                head = handle.read(1024)
                if _looks_binary(head):
                    continue  # binaire : ni compté comme balayé, ni cherché
                handle.seek(0)
                data = handle.read(MAX_SEARCH_FILE_BYTES + 1)
            scanned_files += 1
        except OSError:
            continue  # fichier illisible -> ignoré silencieusement

        text = data.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(
                    {
                        "path": item.relative_to(root).as_posix(),
                        "line_number": line_number,
                        "text": truncate_output(line.strip(), DEFAULT_LINE_PREVIEW_CHARS),
                    }
                )
                if len(matches) >= max_results:
                    truncated = True
                    break

    matches.sort(key=lambda m: (m["path"], m["line_number"]))
    return {
        "pattern": str(pattern),
        "search_path": str(base),
        "scanned_files": scanned_files,
        "skipped_oversized_files": skipped_oversized,
        "match_count": len(matches),
        "truncated": truncated,
        "matches": matches[:max_results],
    }


# --- tail ---------------------------------------------------------------------------
def tail_file(path: str, lines: int = 50) -> str:
    """Dernières `lines` lignes d'un fichier texte (lecture arrière bornée à
    256 Ko : adapté aux logs qui grossissent sans rien saturer)."""
    target = safe_resolve(path, must_exist=True)
    if not target.is_file():
        raise IsADirectoryError(f"Pas un fichier : {target}")
    lines = max(1, min(int(lines), 500))

    with open(target, "rb") as handle:
        handle.seek(0, 2)
        position = handle.tell()
        buffer = b""
        hit_start = position == 0
        while buffer.count(b"\n") < lines:
            step = min(TAIL_CHUNK_BYTES, position)
            if step == 0:
                hit_start = True
                break
            position -= step
            handle.seek(position)
            buffer = handle.read(step) + buffer
            if position == 0:
                hit_start = True
                break
            if len(buffer) >= MAX_TAIL_BUDGET_BYTES:
                break

    text = buffer.decode("utf-8", errors="replace")
    selected = "\n".join(text.splitlines()[-lines:])
    if not hit_start:
        selected = (
            f"[tronqué : seuls les derniers {len(buffer)} octets ont été lus]\n{selected}"
        )
    return truncate_output(selected)


# --- append --------------------------------------------------------------------------
def append_file(path: str, content: str) -> str:
    """Ajoute `content` à la fin d'un fichier DANS la sandbox (crée le fichier
    et ses parents si nécessaire — contrairement à write_file qui écrase)."""
    target = safe_resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="" : écriture verbatim, pas de traduction \n -> \r\n sous Windows.
    with open(target, "a", encoding="utf-8", newline="") as handle:
        handle.write(content)
    return f"Contenu ajouté à : {target}"


# --- horodatage ------------------------------------------------------------------------
def now(utc: bool = True) -> str:
    """Horodatage courant ISO lisible ('2026-08-25 14:03:27+00:00')."""
    if utc:
        return datetime.now(timezone.utc).isoformat(sep=" ", timespec="seconds")
    return datetime.now().astimezone().isoformat(sep=" ", timespec="seconds")