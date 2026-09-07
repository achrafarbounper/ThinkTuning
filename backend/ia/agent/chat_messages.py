"""Normalisation des messages de chat pour les serveurs à ALTERNANCE STRICTE.

Certaines familles de modèles (Mistral notamment) imposent, via leur template
Jinja de chat, une alternance stricte des rôles :

    « Conversation roles must alternate user/assistant/user/assistant/... »

Deux messages consécutifs de même rôle (deux « user », deux « system », deux
« assistant »…) font alors rejeter la requête en 400 par le serveur, même si
le contenu est parfaitement valide. Ce fut la cause du crash des workers
multi-agents : la conclusion de fin de budget était ajoutée comme un second
« user » juste après le message « Dernier résultat : … ».

``ensure_strict_alternance`` est la GARDE STRUCTURELLE unique : elle fusionne
les messages adjacents de même rôle (contenu préservé, ordre conservé) et est
appliquée au point de passage unique de l'agent (``AgentCore._call_llm``),
avant CHAQUE appel LLM — streaming ou non. Fonction pure : l'entrée n'est
jamais mutée, aucune I/O, testable hors réseau (KISS + testabilité).
"""

from __future__ import annotations

from typing import Any

# Rôles valides du protocole de chat OpenAI-compatible. Tout autre rôle
# (ex. « tool », « function ») est ÉCARTÉ : l'agent n'utilise pas ce protocole
# et un rôle inattendu ferait rejeter la requête par les templates stricts.
_VALID_ROLES = ("system", "user", "assistant")


def ensure_strict_alternance(
    messages: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Renvoie une copie de ``messages`` sans deux rôles identiques adjacents.

    Règles (idempotent : normaliser deux fois ne change rien) :
      - les messages adjacents de MÊME rôle sont FONDUS (contenus séparés par
        un double saut de ligne — le premier garde la position d'origine) ;
      - les rôles sont normalisés (trim + minuscules) et filtrés sur
        ``system`` / ``user`` / ``assistant`` ;
      - les messages sans contenu exploitable (vide / blanc) sont retirés :
        ils ne portent rien et créent une pseudo-alternance cassée
        (ex. ``user → assistant("") → user`` vue comme deux « user »
        adjacents après retrait) ;
      - les entrées non-dict sont ignorées (robustesse face aux appelsants
        hétérogènes, même convention que ``_normalize_history_messages``).

    Args:
        messages: liste de messages ``{"role": ..., "content": ...}``.

    Returns:
        Nouvelle liste (jamais l'objet d'entrée) strictement alternée.
    """
    normalized: list[dict[str, Any]] = []
    for raw in messages or []:
        if not isinstance(raw, dict):
            continue
        role = str(raw.get("role") or "").strip().lower()
        if role not in _VALID_ROLES:
            continue
        content = raw.get("content")
        text = content if isinstance(content, str) else str(content or "")
        if not text.strip():
            continue
        if normalized and normalized[-1]["role"] == role:
            normalized[-1]["content"] = f"{normalized[-1]['content']}\n\n{text}"
        else:
            normalized.append({"role": role, "content": text})
    return normalized
