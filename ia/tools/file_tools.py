"""Outils d'écriture et d'inspection de fichiers pour l'agent IA.

Complète system_tools (ls/cat/find) et search_tools (grep/tail/append) avec ce
qui relève de la PRODUCTION de fichiers et de leur inspection fine :

    - write_file       : écriture (atomique, taille bornée, écrase) ;
    - file_info        : métadonnées (type, taille, dates, encodage, lignes) ;
    - file_checksum    : empreinte md5/sha1/sha256/sha512 (mémoire constante) ;
    - head_file        : premières lignes (pendant symétrique de tail_file) ;
    - count_lines      : décompte lignes / mots / caractères / octets ;
    - touch            : crée un fichier vide ou rafraîchit sa date ;
    - write_json       : sérialise dict/liste en JSON UTF-8 indenté ;
    - read_json        : lit et parse un JSON (message d'erreur clair sinon) ;
    - find_duplicates  : fichiers doublons par empreinte du contenu ;
    - split_file       : découpe un gros fichier en morceaux numérotés ;
    - dedupe_lines     : supprime les lignes dupliquées (avec sauvegarde .bak).

Sécurité : tous les chemins passent par `sandbox.safe_resolve` (aucune sortie
de la racine), les écritures sous `.git` ou sur la racine sont interdites,
et les écritures sont ATOMIQUES (jamais de fichier partiel visible).
"""

import hashlib
import json
import os
import shutil
import tempfile
from itertools import islice
from pathlib import Path
from typing import Iterator

from .sandbox import get_sandbox_root, iso_from_timestamp, safe_resolve

DEFAULT_MAX_WRITE_BYTES = 5 * 1024 * 1024  # 5 Mo, surchargeable via AGENT_MAX_WRITE_BYTES
MAX_LINES_PER_SPLIT = 10_000      # borne haute pour le découpage (split_file)
HASH_CHUNK_BYTES = 64 * 1024      # lecture par blocs (mémoire constante)

# Dossiers systématiquement exclus du balayage (cohérent avec search_tools).
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "venv", ".venv", "node_modules"}


# --- Helpers internes ----------------------------------------------------------------
def _get_max_write_bytes(override) -> int:
    """Borne max de taille d'écriture : valeur explicite, sinon env, sinon défaut."""
    if override is not None:
        return int(override)
    raw = os.getenv("AGENT_MAX_WRITE_BYTES")
    if raw is not None and raw.strip().isdigit():
        return int(raw)
    return DEFAULT_MAX_WRITE_BYTES


def _ensure_writable(target: Path) -> None:
    """Garde-fous communs à toute écriture : racine, ``.git``, cible = dossier."""
    root = get_sandbox_root()
    if target == root:
        raise PermissionError("Écriture sur la racine de la sandbox interdite.")
    if ".git" in target.relative_to(root).parts:
        raise PermissionError("Écriture sous '.git' interdite.")
    if target.exists() and target.is_dir():
        raise IsADirectoryError(f"Chemin existant qui est un dossier : {target}")
    target.parent.mkdir(parents=True, exist_ok=True)


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    """Écrit `data` de façon atomique : temporaire dans le même dossier puis rename.

    Garantit qu'un processus interrompu ne laisse jamais un fichier partiel
    visible sous son nom final.
    """
    fd, tmp_path = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_path, str(target))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _hash_file(target: Path, algo: str) -> str:
    """Empreinte `algo` d'un fichier, par blocs (mémoire constante)."""
    hasher = hashlib.new(algo)
    with open(target, "rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _count_lines_binary(target: Path) -> int:
    """Nombre de lignes d'un fichier texte (même définition que count_lines)."""
    count = 0
    with open(target, "rb") as handle:
        for _ in handle:
            count += 1
    return count


# --- write ---------------------------------------------------------------------------
def write_file(filename: str, content: str, max_bytes: int | None = None) -> str:
    """Écrit `content` DANS la sandbox (rétro-compatible : chemins relatifs
    résolus depuis la racine autorisée, évasion hors racine refusée).

    - Écriture ATOMIQUE (temp + rename) : jamais de fichier partiel ;
    - encodage UTF-8 et fins de ligne ``\\n`` (déterministes) ;
    - taille plafonnée (AGENT_MAX_WRITE_BYTES ou `max_bytes`) ;
    - garde-fous : la racine et ``.git`` ne sont jamais écrasables.
    Les dossiers parents sont créés au besoin.
    """
    if not isinstance(content, str):
        raise TypeError(f"'content' doit être une chaîne (str), pas {type(content).__name__}.")
    target = safe_resolve(filename)
    _ensure_writable(target)

    data = content.encode("utf-8")
    limit = _get_max_write_bytes(max_bytes)
    if limit > 0 and len(data) > limit:
        raise ValueError(f"Contenu trop grand ({len(data)} octets > limite {limit}).")

    existed = target.exists()
    _atomic_write_bytes(target, data)
    verb = "écrasé" if existed else "écrit"
    return f"Fichier {verb}: {target} ({len(data)} octets)"


# --- inspection ----------------------------------------------------------------------
def _detect_encoding(target: Path) -> str:
    """Devine l'encodage d'un fichier texte : BOM UTF-8, sinon ascii/utf-8."""
    with open(target, "rb") as handle:
        head = handle.read(4)
    if head.startswith(b"\xef\xbb\xbf"):
        return "utf-8-bom"
    if head[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        return "utf-16"
    return "utf-8"


def file_info(path: str) -> dict:
    """Métadonnées d'un fichier ou dossier (type, taille, dates, encodage, lignes)."""
    target = safe_resolve(path, must_exist=True)

    if not target.is_file():
        if target.is_dir():
            stat = target.stat()
            return {
                "path": str(target),
                "type": "dir",
                "size_bytes": None,
                "modified": iso_from_timestamp(stat.st_mtime),
                "created": iso_from_timestamp(stat.st_ctime),
            }
        return {"path": str(target), "type": "other"}

    stat = target.stat()
    info = {
        "path": str(target),
        "type": "file",
        "extension": target.suffix or None,
        "size_bytes": stat.st_size,
        "modified": iso_from_timestamp(stat.st_mtime),
        "created": iso_from_timestamp(stat.st_ctime),
    }
    if stat.st_size <= DEFAULT_MAX_WRITE_BYTES:
        info["encoding"] = _detect_encoding(target)
        info["lines"] = _count_lines_binary(target)
    else:
        info["encoding"] = None
        info["lines"] = None
    return info


# --- checksum ------------------------------------------------------------------------
def file_checksum(path: str, algo: str = "sha256") -> dict:
    """Empreinte (hash) d'un fichier : md5, sha1, sha256 (défaut) ou sha512.

    Lecture par blocs → mémoire constante, fichiers énormes OK. Utile pour
    vérifier l'intégrité d'un téléchargement ou détecter des doublons.
    """
    target = safe_resolve(path, must_exist=True)
    if not target.is_file():
        raise IsADirectoryError(f"Pas un fichier : {target}")
    algo = algo.lower().strip()
    if algo not in {"md5", "sha1", "sha256", "sha512"}:
        raise ValueError(f"Algorithme inconnu : {algo} (md5, sha1, sha256, sha512).")
    digest = _hash_file(target, algo)
    return {
        "path": str(target),
        "algorithm": algo,
        "size_bytes": target.stat().st_size,
        "hexdigest": digest,
    }


# --- head / count --------------------------------------------------------------------
def head_file(path: str, max_lines: int = 50) -> dict:
    """Premières lignes d'un fichier texte (pendant symétrique de tail_file)."""
    target = safe_resolve(path, must_exist=True)
    if not target.is_file():
        raise IsADirectoryError(f"Pas un fichier : {target}")
    max_lines = max(1, int(max_lines))

    out_lines: list[str] = []
    truncated = False
    with open(target, encoding="utf-8", errors="replace") as handle:
        for i, line in enumerate(handle):
            if i >= max_lines:
                truncated = True
                break
            out_lines.append(line.rstrip("\n"))

    return {
        "path": str(target),
        "max_lines": max_lines,
        "returned_lines": len(out_lines),
        "truncated": truncated,
        "lines": out_lines,
    }


def count_lines(path: str) -> dict:
    """Décompte lignes, mots, caractères et octets (équivalent `wc`)."""
    target = safe_resolve(path, must_exist=True)
    if not target.is_file():
        raise IsADirectoryError(f"Pas un fichier : {target}")

    lines = words = chars = byte_ = 0
    with open(target, "rb") as handle:
        for raw in handle:  # itère sur les lignes octets, quelle que soit la taille
            lines += 1
            byte_ += len(raw)
            text = raw.decode("utf-8", errors="replace")
            chars += len(text)
            words += len(text.split())

    return {
        "path": str(target),
        "lines": lines,
        "words": words,
        "characters": chars,
        "bytes": byte_,
    }


# --- touch ----------------------------------------------------------------------------
def touch(path: str) -> str:
    """Crée un fichier vide ou rafraîchit sa date de modification (sans écraser)."""
    target = safe_resolve(path)
    _ensure_writable(target)
    if target.exists():
        os.utime(target)
        return f"Date rafraîchie : {target}"
    _atomic_write_bytes(target, b"")
    return f"Fichier vide créé : {target}"


# --- JSON -----------------------------------------------------------------------------
def write_json(path: str, data, indent: int = 2) -> str:
    """Sérialise `data` (dict ou list) en JSON UTF-8 indenté DANS la sandbox.

    Pratique pour les configs / résultats d'expériences : structure lisible,
    re-fiable par read_json. Respecte les mêmes garde-fous que write_file.
    """
    if not isinstance(data, (dict, list)):
        raise TypeError(f"'data' doit être dict ou list, pas {type(data).__name__}.")
    indent = max(0, int(indent))
    text = json.dumps(data, ensure_ascii=False, indent=indent) + "\n"
    return write_file(path, text)


def read_json(path: str) -> dict:
    """Lit et parse un fichier JSON ; message d'erreur précis si invalide."""
    target = safe_resolve(path, must_exist=True)
    if not target.is_file():
        raise IsADirectoryError(f"Pas un fichier : {target}")
    text = target.read_text(encoding="utf-8", errors="replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON invalide dans {target} : {exc.msg} (ligne {exc.lineno}).") from exc
    return {"path": str(target), "data": data}


# --- duplicates / split / dedupe ------------------------------------------------------
def _iter_files(base: Path) -> Iterator[Path]:
    """Itère sur les fichiers non vides sous `base`, en sautant les exclusions."""
    for entry in base.iterdir():
        if entry.is_dir():
            if entry.name not in _SKIP_DIRS:
                yield from _iter_files(entry)
        elif entry.is_file() and entry.stat().st_size > 0:
            yield entry


def find_duplicates(path: str = ".") -> dict:
    """Détecte les fichiers au contenu identique (par empreinte) sous `path`.

    Ignore ``.git``, ``__pycache__``, ``venv``, ``node_modules``. Retourne un
    groupe {sha256, size_bytes, paths} par ensemble de fichiers dupliqués.
    """
    base = safe_resolve(path, must_exist=True)
    if not base.is_dir():
        raise NotADirectoryError(f"Pas un répertoire : {base}")
    root = get_sandbox_root()

    groups: dict[str, list[str]] = {}
    files_scanned = 0
    for item in _iter_files(base):
        digest = _hash_file(item, "sha256")
        groups.setdefault(digest, []).append(str(item.relative_to(root).as_posix()))
        files_scanned += 1

    duplicates = {digest: plist for digest, plist in groups.items() if len(plist) > 1}
    return {
        "search_path": str(base),
        "files_scanned": files_scanned,
        "duplicate_groups": len(duplicates),
        "duplicate_files": sum(len(plist) for plist in duplicates.values()),
        "groups": [
            {
                "sha256": digest,
                "size_bytes": (root / plist[0]).stat().st_size,
                "paths": plist,
            }
            for digest, plist in sorted(duplicates.items())
        ],
    }


def split_file(path: str, max_lines: int = 1000) -> dict:
    """Découpe un gros fichier en morceaux numérotés (max_lines lignes chacun).

    Les morceaux portent le nom de l'original suffixé ``_<n>_part.txt`` ; le
    fichier source n'est jamais modifié.
    """
    target = safe_resolve(path, must_exist=True)
    if not target.is_file():
        raise IsADirectoryError(f"Pas un fichier : {target}")
    max_lines = max(1, min(int(max_lines), MAX_LINES_PER_SPLIT))

    parts: list[str] = []
    part_index = 0
    with open(target, encoding="utf-8", errors="replace") as handle:
        while True:
            batch = list(islice(handle, max_lines))
            if not batch:
                break
            part_index += 1
            out_name = f"{target.stem}_part_{part_index}.txt"
            out_path = safe_resolve(str(target.parent / out_name))
            text = "\n".join(line.rstrip("\n") for line in batch) + "\n"
            _atomic_write_bytes(out_path, text.encode("utf-8"))
            parts.append(out_name)

    return {
        "path": str(target),
        "max_lines": max_lines,
        "part_count": len(parts),
        "parts": parts,
    }


def dedupe_lines(path: str, keep: str = "first") -> dict:
    """Supprime les lignes dupliquées d'un fichier (dans la sandbox).

    - `keep="first"` conserve la 1re occurrence, `keep="last"` la dernière ;
    - une sauvegarde ``<nom>.bak`` est créée avant modification ;
    - encodage UTF-8, fins de lignes normalisées.
    """
    target = safe_resolve(path, must_exist=True)
    if not target.is_file():
        raise IsADirectoryError(f"Pas un fichier : {target}")

    keep = keep.lower().strip()
    if keep not in {"first", "last"}:
        raise ValueError("'keep' doit être 'first' ou 'last'.")

    with open(target, encoding="utf-8", errors="replace") as handle:
        lines = [ln.rstrip("\n") for ln in handle]

    if keep == "first":
        kept: list[str] = []
        seen: set[str] = set()
        for ln in lines:
            if ln not in seen:
                seen.add(ln)
                kept.append(ln)
    else:  # "last" -> on conserve la dernière occurrence, en préservant l'ordre
        last_index: dict[str, int] = {}
        for idx, ln in enumerate(lines):
            last_index[ln] = idx
        kept = [lines[idx] for idx in sorted(set(last_index.values()))]

    backup = target.with_name(target.name + ".bak")
    shutil.copy2(target, backup)
    text = "\n".join(kept) + ("\n" if kept else "")
    _atomic_write_bytes(target, text.encode("utf-8"))

    return {
        "path": str(target),
        "original_lines": len(lines),
        "remaining_lines": len(kept),
        "removed_duplicates": len(lines) - len(kept),
        "backup": str(backup),
    }
