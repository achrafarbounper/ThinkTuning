"""Mémoire conversationnelle — use-case extrait de ``api/routes/agent.py``.

Historiquement, le rechargement de l'historique de session et la persistance
d'un échange vivaient dans la couche ``api/``. Ce sont des responsabilités
métier : elles migrent ici, et la route n'en conserve que des délégations
minces (compatibilité monkeypatch des tests préservée).

Note de migration : les imports legacy (``ia.agent.context``,
``core.session_store``) seront absorbés par des ports dédiés (``ContextPort``,
``SessionStorePort``) lors de la fusion du legacy dans Core v2.
"""

from __future__ import annotations

from core.feature_flags import flag
from core.session_store import get_session_store
from ia.agent.context import (
    DEFAULT_HISTORY_BUDGET_TOKENS,
    format_memory_note,
    optimize_history,
    summarize_conversation,
    update_memory_summary,
)

# Nombre maximal de paires user/assistant rejouées comme contexte de session
# (mémoire de conversation en mode Agent). Borné pour ne pas exploser la
# fenêtre de contexte LLM sur les longues conversations.
MAX_SESSION_CONTEXT_TURNS = 5


def load_session_history(
    session_id: str | None,
    resume_request_id: str | None,
) -> list[dict]:
    """Recharge les messages précédents d'une session pour nourrir le contexte.

    Mémoire de session en mode Agent : sans cela chaque tour est atomique et
    l'agent « oublie » ce qui a été dit avant (ex. le nom de l'utilisateur).

    Règles :
        - ``session_id`` absent -> historique vide (pas de conversation à relire) ;
        - ``resume_request_id`` présent -> historique vide : la reprise relance
          l'état interne de l'agent, pas besoin de rejouer la conversation ;
        - sinon, on relit les ``get_messages`` de la session et on garde les
          ``MAX_SESSION_CONTEXT_TURNS`` dernières paires. Le tour courant n'y
          figure pas encore (``persist_exchange`` écrit après coup).
        - Les ``tool_calls`` sont écartés (pas de JSON d'outils en contexte).

    Retourne une liste de ``{"role": "user"|"assistant", "content": str}``,
    vide par défaut.
    """
    if not session_id or resume_request_id:
        return []
    try:
        store = get_session_store()
        if store.get_session(session_id) is None:
            messages = []  # session inconnue : la mémoire inter-sessions
            # (Phase C) pourra tout de même être injectée plus bas.
        else:
            messages = store.get_messages(session_id, limit=500)
    except Exception:  # pragma: no cover - mémoire optionnelle, jamais bloquante
        return []
    kept: list[dict] = []
    for message in messages:
        role = str(message.get("role") or "").strip().lower()
        content = message.get("content")
        text = content if isinstance(content, str) else str(content or "")
        if role not in ("user", "assistant") or not text.strip():
            continue
        kept.append({"role": role, "content": text.strip()})
    # Conserve seulement les N dernières paires user/assistant (les plus récents).
    kept = kept[-MAX_SESSION_CONTEXT_TURNS * 2 :]

    # Phase C (flag ``AGENT_CONTEXT``) : budget en jetons avec résumé LLM des
    # tours débordants, puis mémoire inter-sessions si la session est neuve.
    # Sans le flag : comportement historique strictement préservé.
    if not flag("context"):
        return kept
    try:
        import os as _os

        from core.agent_cache import get_agent_runner

        budget = int(_os.getenv("AGENT_CONTEXT_BUDGET_TOKENS", "0")) or (
            DEFAULT_HISTORY_BUDGET_TOKENS
        )
        def summarize_fn(transcript: list[dict]) -> str:
            return summarize_conversation(
                get_agent_runner().core.llm, transcript
            )  # injection paresseuse, jamais appelée si pas de débordement
        optimized, _meta = optimize_history(kept, max_tokens=budget, summarize_fn=summarize_fn)
    except Exception:  # pragma: no cover - mémoire optionnelle, jamais bloquante
        optimized = kept
    if not optimized:
        try:
            note = format_memory_note(store.get_memory("global"))
            if note is not None:
                optimized = [note]
        except Exception:  # pragma: no cover
            pass
    return optimized


def persist_exchange(
    session_id: str | None,
    prompt: str,
    answer: str,
    tool_events: list[dict] | None = None,
) -> None:
    """Journalise l'échange (user + assistant) dans la session demandée.

    Best-effort : une session absente ou une erreur de base ne doivent jamais
    faire échouer le tour de chat lui-même.
    """
    if not session_id:
        return
    try:
        store = get_session_store()
        store.append_message(session_id, "user", prompt)
        store.append_message(session_id, "assistant", answer or "", tool_calls=tool_events)
        # Phase C (flag ``AGENT_CONTEXT``) : mémoire glissante inter-sessions.
        # Résumé déterministe (sans LLM) conservé sous la clé « global » et
        # réinjecté uniquement dans les NOUVELLES sessions (cf.
        # load_session_history). Best-effort : ne casse jamais le tour.
        if flag("context"):
            previous = store.get_memory("global")
            store.save_memory(
                "global",
                update_memory_summary(previous, prompt, answer or ""),
            )
    except Exception:  # pragma: no cover - persistance optionnelle
        pass
