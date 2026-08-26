import re
from pathlib import Path

from .sandbox import safe_resolve


def write_file(filename: str, content: str) -> str:
    """Écrit un fichier DANS la sandbox (rétro-compatible : chemins relatifs
    résolus depuis la racine autorisée, évasion hors racine refusée).
    Les dossiers parents sont créés au besoin."""
    target = safe_resolve(filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    Path(target).write_text(content, encoding="utf-8")
    return f"Fichier écrit: {target}"


MAX_EDIT_FILE_BYTES = 1 * 1024 * 1024  # 1 Mo : au-delà, réécriture complète via write_file

_EDIT_NO_MATCH_HINT = (
    "'old_text' introuvable dans {path} (0 occurrence). Relis le fichier"
    " (read_file) ou cherche le passage exact (search_in_files), puis copie"
    " le texte EXACT : casse, espaces et retours à la ligne comptent."
)
_EDIT_AMBIGUOUS_HINT = (
    "{n} occurrences de 'old_text' dans {path} : enrichis old_text avec plus"
    " de contexte pour viser UNE seule zone, ou passe replace_all=true si tu"
    " veux remplacer TOUTES les occurrences."
)


def _apply_replacement(content: str, old_text: str, new_text: str,
                       replace_all: bool, display_path: str) -> tuple[str, int]:
    """Logique de substitution exacte (+ repli tolérant aux fins de ligne).

    1) Correspondance littérale de old_text dans content.
    2) Si 0 occurrence et old_text contient \\n : tolérance CRLF — sous
       Windows un « \\n » logique peut être stocké « \\r\\n » ; on recherche
       alors une regex où chaque \\n accepte \\r?\\n, et new_text est adapté
       à la convention locale de chaque zone remplacée (rien d'autre ne bouge).
    Lève ValueError (auto-correction LLM) si introuvable / ambigu.
    """
    count = content.count(old_text)
    if count:
        if count > 1 and not replace_all:
            raise ValueError(
                _EDIT_AMBIGUOUS_HINT.format(n=count, path=display_path)
            )
        return content.replace(old_text, new_text), count

    if "\n" in old_text:
        pattern = r"\r?\n".join(re.escape(part) for part in old_text.split("\n"))
        occurrences = len(re.findall(pattern, content))
        if occurrences == 0:
            raise ValueError(_EDIT_NO_MATCH_HINT.format(path=display_path))
        if occurrences > 1 and not replace_all:
            raise ValueError(
                _EDIT_AMBIGUOUS_HINT.format(n=occurrences, path=display_path)
            )

        def _adapt(match: re.Match) -> str:
            if "\r\n" in match.group(0):
                return new_text.replace("\n", "\r\n")
            return new_text

        updated, done = re.subn(pattern, _adapt, content)
        return updated, done

    raise ValueError(_EDIT_NO_MATCH_HINT.format(path=display_path))


def edit_file(path: str, old_text: str, new_text: str,
              replace_all: bool = False) -> str:
    """Remplace old_text par new_text dans un fichier DANS la sandbox.

    Matching littéral EXACT (0 occurrence -> erreur incitant à relire via
    read_file/search_in_files ; plusieurs occurrences -> erreur demandant
    plus de contexte ou replace_all=true). Tolérance automatique aux fins
    de ligne Windows (\\r\\n vs \\n). Écriture verbatim : le reste du fichier
    est préservé à l'octet près. Retour : nombre de remplacements effectués.
    """
    if not isinstance(old_text, str) or not old_text:
        raise ValueError("'old_text' doit être une chaîne NON VIDE.")
    if not isinstance(new_text, str):
        raise TypeError("'new_text' doit être une chaîne.")
    if old_text == new_text:
        raise ValueError(
            "'old_text' et 'new_text' sont identiques : rien à modifier."
        )

    target = safe_resolve(path, must_exist=True)
    if not target.is_file():
        raise IsADirectoryError(f"Pas un fichier : {target}")
    size = target.stat().st_size
    if size > MAX_EDIT_FILE_BYTES:
        raise ValueError(
            f"Fichier trop volumineux pour edit_file ({size} octets > "
            f"{MAX_EDIT_FILE_BYTES}). Si c'est voulu, réécris-le en entier "
            "via write_file."
        )
    try:
        with open(target, "r", encoding="utf-8", newline="") as handle:
            content = handle.read()
    except UnicodeDecodeError as exc:
        raise ValueError(f"Fichier non UTF-8, édition refusée : {target}") from exc

    updated, done = _apply_replacement(
        content, old_text, new_text, bool(replace_all), str(target)
    )
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(updated)

    pluriel = "s" if done > 1 else ""
    return f"Fichier modifié ({done} remplacement{pluriel}) : {target}"
