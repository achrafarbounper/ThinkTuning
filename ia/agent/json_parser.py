"""Extraction des blocs JSON dans les réponses du LLM.

Le modèle entoure parfois ses appels d'outils de texte libre, de fences
markdown (```json ... ```) ou empile plusieurs objets sur des lignes
distinctes : cette fonction isole chaque objet JSON complet et valide.

Robustesse (correctif du bug « le contenu du fichier ne s'affiche pas ») :
    - l'état « dans une chaîne » est réinitialisé pour CHAQUE candidat ``{…}``,
      donc un guillemet perdu dans la prose qui précède ne corrompt plus
      l'analyse du JSON qui suit (bug historique de l'analyseur à état global) ;
    - un objet mal formé est ignoré sans empêcher la découverte des suivants ;
    - aucune exception ne remonte : une réponse sans JSON valide renvoie [].
"""

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

