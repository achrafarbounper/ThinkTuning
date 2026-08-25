from pathlib import Path

from tools.sandbox import safe_resolve


def write_file(filename: str, content: str) -> str:
    """Écrit un fichier DANS la sandbox (rétro-compatible : chemins relatifs
    résolus depuis la racine autorisée, évasion hors racine refusée).
    Les dossiers parents sont créés au besoin."""
    target = safe_resolve(filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_text(content, encoding="utf-8")
    return f"Fichier écrit: {target}"
