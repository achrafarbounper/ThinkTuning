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


def _find_matching_brace(text: str, start: int) -> int:
    """Indice du ``}`` fermant le ``{`` situé à ``start``, ou -1.

    Ignore les accolades et guillemets figurant À L'INTÉRIEUR des chaînes
    JSON (gestion correcte des échappements ``\\"`` et ``\\\\``).
    """
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    return -1


def extract_json_blocks(text: str) -> list:
    """Retourne la liste des objets JSON complets trouvés dans ``text``."""
    blocks: list = []
    index = 0
    while index < len(text):
        if text[index] == "{":
            end = _find_matching_brace(text, index)
            if end != -1:
                candidate = text[index : end + 1]
                try:
                    blocks.append(json.loads(candidate))
                    # Objet valide consommé : on reprend APRÈS sa fermeture
                    # (les sous-objets imbriqués ne sont pas retournés seuls).
                    index = end + 1
                    continue
                except json.JSONDecodeError:
                    pass  # mal formé : on avance d'un caractère et on continue
        index += 1
    return blocks

