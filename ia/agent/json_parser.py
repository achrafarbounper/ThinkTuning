import json
import re

# Séquence d'échappement INVALIDE en JSON strict : un backslash NON suivi d'un
# caractère autorisé (\" \\ \/ \b \f \n \r \t \uXXXX). Les LLM oublient souvent
# de doubler les backslashes des chemins Windows (ex: D:\ThinkTuning\configs).
_INVALID_ESCAPE = re.compile(r'\\(?!["\\/bfnrtu])')
# Virgule traînante avant } ou ] (autre erreur classique des modèles).
_TRAILING_COMMA = re.compile(r",\s*(?=[}\]])")


def _parse_lenient(candidate: str):
    """Parse en mode strict, puis retente après réparation des erreurs courantes."""
    try:
        return json.loads(candidate)
    except ValueError:
        pass
    repaired = _INVALID_ESCAPE.sub("\\\\\\\\", candidate)
    repaired = _TRAILING_COMMA.sub("", repaired)
    try:
        return json.loads(repaired)
    except ValueError:
        return None


def extract_json_blocks(text: str):
    """Extrait les objets JSON {...} d'une réponse LLM, tolérant aux fautes.

    Les blocs illisibles même après réparation sont ignorés silencieusement
    (comportement historique).
    """
    blocks = []
    buffer = ""
    depth = 0
    in_string = False
    escape = False

    for char in text:
        if char == '"' and not escape:
            in_string = not in_string

        if char == "\\" and not escape:
            escape = True
        else:
            escape = False

        if not in_string:
            if char == "{":
                depth += 1
            if depth > 0:
                buffer += char
            if char == "}":
                depth -= 1
                if depth == 0:
                    parsed = _parse_lenient(buffer)
                    if parsed is not None:
                        blocks.append(parsed)
                    buffer = ""
        else:
            if depth > 0:
                buffer += char

    return blocks
