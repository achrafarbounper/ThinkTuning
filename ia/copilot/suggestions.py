"""Suggestions intelligentes de type « GitHub Copilot » (Phase D).

Trois briques composables, toutes utilisables hors API (tests offline) :

    1. ``suggest_for_context(...)`` — à partir du dernier message utilisateur
       (et d'un brouillon éventuel), classe les outils du registre par
       pertinence (réutilise ``tools.tool_discovery``), applique le boost
       d'apprentissage de ``copilot.feedback``, et déduit pour chaque outil
       un squelette d'arguments (auto-complétion des paramètres obligatoires,
       pré-remplissage depuis le brouillon) ;
    2. ``nl_to_tool(query)`` — mapping langage naturel -> outil le plus
       probable + squelette d'arguments ;
    3. ``complete_text(llm, ...)`` — complétion en ligne : laisse le LLM
       continuer le brouillon de l'utilisateur (échec doux -> chaîne vide).

Aucune brique n'appelle le réseau : seul ``complete_text`` utilise le client
LLM fourni par l'appelant (FakeLLM dans les tests).
"""

import re

try:  # import relatif dual (paquet ia.* ou modules top-level via sys.path)
    from ..tools.tool_discovery import suggest_tools
    from ..tools.tool_registry import REQUIRED_ARGS
except ImportError:  # pragma: no cover
    from tools.tool_discovery import suggest_tools
    from tools.tool_registry import REQUIRED_ARGS

from .feedback import get_feedback_store


# --- Squelette d'arguments ----------------------------------------------------

_QUOTED_RE = re.compile(r"[\"'«“]([^\"'»”]{1,200})[\"'»”]")


def _quoted_values(text: str) -> list[str]:
    """Valeurs entre guillemets d'un texte (candidats au pré-remplissage)."""
    return [m.strip() for m in _QUOTED_RE.findall(text or "") if m.strip()]


def args_skeleton(tool: str, draft: str = "") -> dict:
    """Arguments obligatoires d'un outil, pré-remplis quand c'est évident.

    - un seul argument obligatoire + une valeur quotée dans le brouillon
      -> pré-rempli ;
    - sinon, clés obligatoires à chaîne vide (l'UI les affiche comme
      champs à compléter).
    """
    required = list(REQUIRED_ARGS.get(tool, ()))
    skeleton = {k: "" for k in required}
    values = _quoted_values(draft)
    if len(required) == 1 and len(values) >= 1:
        skeleton[required[0]] = values[0]
    return skeleton


# --- Suggestions de contexte ---------------------------------------------------


def suggest_for_context(
    messages: list[dict] | None = None,
    draft: str = "",
    query: str = "",
    k: int = 3,
) -> dict:
    """Suggestions d'outils pour la conversation en cours.

    ``messages`` : historique ``[{role, content}, ...]`` (le dernier message
    utilisateur sert de requête si ``query`` est vide). ``draft`` : brouillon
    en cours de saisie (affiné la requête et pré-remplit les arguments).

    Retourne ``{"query", "suggestions": [{tool, score, base_score, reasons,
    required_args, args}], ...}`` — suggestions triées par score ajusté
    (score lexical + boost d'apprentissage), score > 0 uniquement.
    """
    if not query:
        query = _last_user_text(messages or [])
    query = f"{query} {draft}".strip()
    if not query:
        return {"query": "", "suggestions": []}

    store = get_feedback_store()
    base = suggest_tools(query, k=max(k * 2, k + 2))
    suggestions: list[dict] = []
    for item in base:
        tool = item["tool"]
        boost = store.boost(tool)
        adjusted = max(0.0, min(1.0, item["score"] + boost))
        if adjusted <= 0:
            continue
        suggestions.append({
            "tool": tool,
            "score": round(adjusted, 3),
            "base_score": item["score"],
            "reasons": item.get("reasons", []),
            "required_args": list(REQUIRED_ARGS.get(tool, ())),
            "args": args_skeleton(tool, draft),
        })
    suggestions.sort(key=lambda s: (-s["score"], s["tool"]))
    return {"query": query, "suggestions": suggestions[: max(1, int(k))]}


def _last_user_text(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user" and (message.get("content") or "").strip():
            return str(message["content"]).strip()
    return ""


# --- Mapping langage naturel -> outil ------------------------------------------


def nl_to_tool(query: str) -> dict | None:
    """Meilleur outil pour un besoin en langage naturel (ou None si aucun).

    Retourne ``{"tool", "score", "args"}`` avec le squelette d'arguments.
    """
    suggestions = suggest_for_context(query=query, k=1)
    items = suggestions.get("suggestions") or []
    if not items:
        return None
    best = items[0]
    return {
        "tool": best["tool"],
        "score": best["score"],
        "args": best["args"],
    }


# --- Complétion en ligne --------------------------------------------------------

_COMPLETION_SYSTEM = (
    "Tu complètes le brouillon en cours de l'utilisateur. Réponds UNIQUEMENT "
    "par la suite la plus probable du texte (une phrase au plus, même langue, "
    "sans préambule, sans répéter le brouillon). Si rien ne s'impose, réponds "
    "par une chaîne vide."
)


def complete_text(llm, messages: list[dict] | None, draft: str) -> str:
    """Suite probable d'un brouillon via le LLM fourni (échec doux -> '')."""
    draft = (draft or "").strip()
    if not draft:
        return ""
    history = [m for m in (messages or []) if m.get("content")]
    call_messages = [{"role": "system", "content": _COMPLETION_SYSTEM}]
    call_messages.extend(history[-4:])
    call_messages.append({
        "role": "user",
        "content": f"Brouillon en cours : « {draft} »",
    })
    try:
        completion = str(llm.call(call_messages) or "").strip()
    except Exception:
        return ""
    return completion
