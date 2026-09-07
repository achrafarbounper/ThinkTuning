"""Wrapper strangler : ``ContextPort`` par délégation au module legacy.

Sous-classe les fonctions pures de ``ia/agent/context.py`` (mêmes signatures)
en les présentant comme méthodes — manière de déplacer le contrat côté domaine
SANS réécrire l'implémentation (coexistence Core v1 / v2). Le comportement est
strictement identique au v1 ; un adaptateur « propre » pourra le remplacer plus
tard sans toucher aux use-cases.
"""

from __future__ import annotations

from app.domain.ports import ContextPort, Message
from ia.agent import context as _legacy


class LegacyContextProvider:
    """Adapter ``ContextPort`` aligné sur ``ia/agent/context.py`` (mode étrangler)."""

    def estimate_tokens(self, text: str) -> int:
        return _legacy.estimate_tokens(text)

    def optimize_history(
        self,
        messages: list[Message],
        max_tokens: int = 1200,
        summarize_fn=None,
    ) -> tuple[list[Message], dict]:
        return _legacy.optimize_history(
            messages, max_tokens=max_tokens, summarize_fn=summarize_fn
        )

    def update_memory_summary(
        self,
        previous_summary: str,
        prompt: str,
        answer: str,
        max_chars: int = 2000,
    ) -> str:
        return _legacy.update_memory_summary(
            previous_summary, prompt, answer, max_chars=max_chars
        )

    def format_memory_note(self, summary: str) -> Message | None:
        return _legacy.format_memory_note(summary)


# Conformité structurelle explicite (rappel : runtime_checkable ne vérifie pas
# les signatures — le test de contrat les introspecte).
_REF: ContextPort = LegacyContextProvider
