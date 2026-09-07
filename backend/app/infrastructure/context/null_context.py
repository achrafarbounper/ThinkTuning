"""Adaptateur déterministe du port ``ContextPort`` (aucune I/O, aucune mutation).

Faux fidèle qui :
    - estime les jetons via l'heuristique du domaine ;\
    - conserve l'historique tel quel (ne résume ni ne tronque) avec une meta
      neutre ;
    - gère la mémoire inter-sessions sans LLM.

Utilisé pour le profil ``AGENT_CONTEXT=0`` et les tests de use-cases hors
réseau. Fait partie des fakes « contrôlés » : comportement reproductible,
zéro dépendance externe.
"""

from __future__ import annotations

from app.domain.ports import ContextPort, Message


class NullContextProvider:
    """Adapter ``ContextPort`` passif : conservation de l'historique telle quelle."""

    def estimate_tokens(self, text: str) -> int:
        if not text:
            return 0
        return max(1, len(text) // 4)

    def optimize_history(
        self,
        messages: list[Message],
        max_tokens: int = 1200,
        summarize_fn=None,
    ) -> tuple[list[Message], dict]:
        total = sum(self.estimate_tokens(m.get("content") or "") for m in messages)
        return list(messages), {
            "kept": len(messages),
            "dropped": 0,
            "summarized": False,
            "estimated_tokens": total,
        }

    def update_memory_summary(
        self,
        previous_summary: str,
        prompt: str,
        answer: str,
        max_chars: int = 2000,
    ) -> str:
        piece = f"User: {prompt}\nAssistant: {answer}\n".strip()
        merged = (previous_summary + "\n" + piece).strip() if previous_summary else piece
        return merged[-max_chars:] if len(merged) > max_chars else merged

    def format_memory_note(self, summary: str) -> Message | None:
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


# Conformité structurelle explicite.
_REF: ContextPort = NullContextProvider
