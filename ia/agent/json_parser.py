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

    L'état d'analyse (dans une chaîne / échappement / profondeur) est
    réinitialisé pour CHAQUE candidat ``{…}`` :
        - un nombre impair de guillemets dans la prose qui précède ne
          corrompt plus l'analyse du JSON qui suit ;
        - un objet mal formé est ignoré sans empêcher la découverte des
          suivants (y compris des objets imbriqués DANS le mal formé) ;
        - le curseur saute après chaque bloc valide, donc un objet déjà
          extrait n'est jamais redétecté via ses accolades internes ;
        - aucune exception ne remonte : sans JSON valide, renvoie [].
    """
    blocks = []
    length = len(text)
    cursor = 0

    while cursor < length:
        brace = text.find("{", cursor)
        if brace < 0:
            break

        # Scan ISOLÉ du candidat démarrant à ``brace`` : état frais.
        depth = 0
        in_string = False
        escape = False
        buffer: list[str] = []
        end = -1

        for index in range(brace, length):
            char = text[index]
            if in_string:
                buffer.append(char)
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
                buffer.append(char)
            elif char == "{":
                depth += 1
                buffer.append(char)
            elif depth > 0:
                buffer.append(char)
                if char == "}":
                    depth -= 1
                    if depth == 0:
                        end = index
                        break

        if end >= 0:
            parsed = _parse_lenient("".join(buffer))
            if parsed is not None:
                blocks.append(parsed)
                cursor = end + 1
                continue
        # Candidat illisible : on tente le ``{`` suivant.
        cursor = brace + 1

    return blocks

