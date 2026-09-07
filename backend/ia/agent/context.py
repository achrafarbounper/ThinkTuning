# project/ia/agent/context.py

"""Gestion avancée du contexte conversationnel — Phase C.

Activé par le flag ``AGENT_CONTEXT`` (cf. ``core/feature_flags.py``). Trois
briques indépendantes, toutes best-effort et sans dépendance externe :

    - ``estimate_tokens(text)`` : estimation de coût en jetons (heuristique
      ~4 caractères/jeton, largement suffisante pour borner un budget) ;
    - ``optimize_history(messages, max_tokens, summarize_fn)`` : fenêtre
      glissante — on garde les tours les plus récents dans le budget ; les
      tours plus anciens sont résumés via ``summarize_fn`` (appel LLM
      optionnel) ou, à défaut, écartés avec une note de troncature ;
    - mémoire inter-sessions : ``format_memory_note`` + helpers SQLite dans
      ``core/session_store.py`` (table ``agent_memory``), qui permettent à
      une NOUVELLE session de retrouver l'essentiel des sessions précédentes.

Toutes les fonctions sont pures côté texte : aucune I/O réseau ici (le
résumé LLM est injecté par l'appelant), ce qui garde le module testable
hors ligne.
"""

from __future__ import annotations

from typing import Callable, Optional

# Budget par défaut du contexte rejoué (jetons estimés). Aligné sur la
# fenêtre des modèles 8B courants (llama3.1:8b) : ~8k jetons au total,
# dont on réserve ici une fraction pour l'historique de conversation.
DEFAULT_HISTORY_BUDGET_TOKENS = 1200

# Taille maximale (caractères) du résumé de mémoire inter-sessions.
MAX_MEMORY_SUMMARY_CHARS = 2000

# Sentinelle insérée quand des tours ont été résumés/écartés.
_TRUNCATION_NOTE = (
    "[Contexte tronqué : {n} tour(s) plus ancien(s) résumé(s) ci-dessus.]"
)


def estimate_tokens(text: str) -> int:
    """Estimation du nombre de jetons d'un texte (heurétique ~4 car/jeton)."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def estimate_messages_tokens(messages: list[dict]) -> int:
    """Somme des jetons estimés d'une liste de messages ``{role, content}``."""
    total = 0
    for message in messages:
        content = message.get("content")
        text = content if isinstance(content, str) else str(content or "")
        total += estimate_tokens(text) + 4  # overhead role/format par message
    return total


def optimize_history(
    messages: list[dict],
    max_tokens: int = DEFAULT_HISTORY_BUDGET_TOKENS,
    summarize_fn: Optional[Callable[[str], str]] = None,
) -> tuple[list[dict], dict]:
    """Fenêtre glissante + résumé éventuel d'un historique de conversation.

    Les messages sont parcourus du plus récent au plus ancien et conservés
    tant que le budget en jetons n'est pas dépassé. Les tours débordants
    (les plus anciens) sont résumés en UN message via ``summarize_fn`` si
    fournie (un seul appel), sinon écartés avec une note de troncature.

    Retourne ``(messages_optimisés, meta)`` — ``meta`` porte ``kept`` /
    ``dropped`` / ``summarized`` / ``estimated_tokens``. L'ordre
    chronologique est préservé.
    """
    if max_tokens <= 0 or not messages:
        return list(messages), {
            "kept": len(messages), "dropped": 0, "summarized": False,
            "estimated_tokens": estimate_messages_tokens(messages),
        }

    kept_rev: list[dict] = []
    budget = max_tokens
    for message in reversed(messages):
        content = message.get("content")
        text = content if isinstance(content, str) else str(content or "")
        cost = estimate_tokens(text) + 4
        if cost > budget:
            break
        kept_rev.append(message)
        budget -= cost

    kept = list(reversed(kept_rev))
    dropped_count = len(messages) - len(kept)
    meta = {
        "kept": len(kept),
        "dropped": dropped_count,
        "summarized": False,
        "estimated_tokens": estimate_messages_tokens(kept),
    }

    if dropped_count <= 0:
        return kept, meta

    if summarize_fn is not None:
        overflow = messages[:dropped_count]
        transcript = "\n".join(
            f"{m.get('role', 'user')}: "
            f"{m.get('content') if isinstance(m.get('content'), str) else ''}"
            for m in overflow
        )
        try:
            summary = str(summarize_fn(transcript)).strip()
        except Exception:  # pragma: no cover - le résumé ne doit jamais casser
            summary = ""
        if summary:
            meta["summarized"] = True
            return (
                [
                    {
                        "role": "user",
                        "content": "[Résumé des tours précédents] " + summary,
                    },
                    {
                        "role": "user",
                        "content": _TRUNCATION_NOTE.format(n=dropped_count),
                    },
                ]
                + kept,
                meta,
            )

    # Sans résumé : note de troncature seule (comportement dégradable).
    return (
        [{"role": "user", "content": _TRUNCATION_NOTE.format(n=dropped_count)}]
        + kept,
        meta,
    )


def summarize_conversation(llm, transcript: str) -> str:
    """Résumé d'une conversation via un appel LLM unique (synchronisé).

    ``llm`` est un client compatible ``call(messages) -> str``. En cas
    d'échec, retourne une chaîne vide : l'appelant dégrade proprement.
    """
    prompt = (
        "Résume en 5 phrases maximum les points essentiels de la conversation "
        "suivante (décisions, faits, préférences de l'utilisateur), en français, "
        "sans mise en forme markdown :\n\n" + transcript[:8000]
    )
    try:
        return str(llm.call([{"role": "user", "content": prompt}]) or "").strip()
    except Exception:  # pragma: no cover - dépend du réseau
        return ""


# --- Mémoire inter-sessions ---------------------------------------------------


def update_memory_summary(
    previous_summary: str,
    prompt: str,
    answer: str,
    max_chars: int = MAX_MEMORY_SUMMARY_CHARS,
) -> str:
    """Résumé glissant, sans LLM : concaténation bornée en caractères.

    Politique volontairement simple et déterministe (testable hors ligne) :
    les échanges les plus récents sont conservés en priorité, les plus
    anciens sortent par la fenêtre.
    """
    piece = f"User: {prompt}\nAssistant: {answer}\n".strip()
    merged = (previous_summary + "\n" + piece).strip() if previous_summary else piece
    if len(merged) > max_chars:
        merged = merged[-max_chars:]
    return merged


def format_memory_note(summary: str) -> Optional[dict]:
    """Message de contexte portant la mémoire des sessions précédentes.

    Retourne ``None`` si le résumé est vide — l'appelant n'injecte alors
    rien (comportement historique strictement préservé).
    """
    text = (summary or "").strip()
    if not text:
        return None
    return {
        "role": "user",
        "content": (
            "[Mémoire des sessions précédentes — rappel de contexte, "
            "n'y réponds pas directement]\n" + text
        ),
    }
