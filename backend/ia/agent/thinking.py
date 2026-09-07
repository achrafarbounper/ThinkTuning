"""Extraction des blocs de réflexion « <think>…</think> » dans les réponses LLM.

Deux sources possibles de réflexion :
    - native (deepseek-r1, qwen3…) : Ollama sépare la trace dans le champ
      ``message.thinking``, mais selon la version du serveur elle peut aussi
      arriver inline dans ``message.content`` entre balises ``<think>`` ;
    - induite par le prompt (mode « Réflexion » de l'agent, voir
      system_prompt.THINKING_PROMPT_SECTION) : le modèle encadre lui-même
      son raisonnement par ``<think>`` et ``</think>``.

Ce module isole proprement ces blocs pour qu'ils n'atteignent ni l'analyseur
JSON des outils (json_parser.extract_json_blocks) ni la réponse affichée.
"""

import re
from typing import Tuple

# Balises tolérantes aux variations de casse et d'espacement (« < think > »).
# Ne capte PAS <thinking> : la fermeture « > » doit suivre directement « think ».
_THINK_OPEN = re.compile(r"<\s*think\s*>", re.IGNORECASE)
_THINK_CLOSE = re.compile(r"<\s*/\s*think\s*>", re.IGNORECASE)


def extract_thinking(text: str) -> Tuple[str, str]:
    """Sépare la réflexion du reste de la réponse.

    Args:
        text: réponse brute du LLM (zéro, un ou plusieurs blocs <think>).

    Returns:
        Tuple ``(texte_nettoye, reflexion)`` : ``reflexion`` concatène les
        blocs trouvés (séparés par une ligne vide), ``texte_nettoye`` est la
        réponse débarrassée des balises et de leur contenu.

    Robustesse (aucune exception ne remonte) :
        - balise fermante absente : tout ce qui suit ``<think>`` est traité
          comme réflexion (génération tronquée en cours de raisonnement) ;
        - plusieurs blocs sont acceptés et concaténés ;
        - une balise fermante orpheline est retirée silencieusement ;
        - sans aucun bloc : ``(texte inchangé, "")``.
    """
    if not text or "<" not in text:
        return text or "", ""

    cleaned_chunks = []
    thinking_parts = []
    cursor = 0

    while True:
        opener = _THINK_OPEN.search(text, cursor)
        if opener is None:
            cleaned_chunks.append(text[cursor:])
            break
        cleaned_chunks.append(text[cursor:opener.start()])

        closer = _THINK_CLOSE.search(text, opener.end())
        if closer is None:
            thinking_parts.append(text[opener.end():])
            break
        thinking_parts.append(text[opener.end():closer.start()])
        cursor = closer.end()

    cleaned = _THINK_CLOSE.sub("", "".join(cleaned_chunks)).strip()
    thinking = "\n\n".join(part.strip() for part in thinking_parts if part.strip())
    return cleaned, thinking