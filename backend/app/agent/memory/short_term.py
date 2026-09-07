"""Mémoire short-term : fenêtre glissante sur les messages de la session.

S'appuie exclusivement sur ``SessionStorePort`` (aucune connaissance du
stockage réel). Rôle dans la boucle agentique : fournir au LLM un contexte
conversationnel borné (la fenêtre) tout en conservant l'historique complet
dans le store (les messages ne sont JAMAIS supprimés ici).

Comportements clés :
    - la fenêtre est exprimée en NOMBRE DE MESSAGES (défaut : aligné sur
      ``AGENT_CONTEXT_LENGTH``/ Settings.agent_context_length via le use-case) ;
    - le premier message utilisateur est toujours conservé (porteur du titre
      et de l'intention initiale — cf. session_store.append_message) ;
    - tolérant aux erreurs du store : la mémoire est un comfort, pas une
      condition du run (dégradation gracieuse, log + liste vide).
"""

from __future__ import annotations

import logging
from typing import Any

from app.domain.ports import Message, SessionStorePort

logger = logging.getLogger("thinktuning.agent.memory")

DEFAULT_WINDOW = 20


class ShortTermMemory:
    """Fenêtre glissante de conversation pour un run agent."""

    def __init__(self, store: SessionStorePort, session_id: str, window: int = DEFAULT_WINDOW):
        if window < 1:
            raise ValueError("window doit être >= 1")
        self._store = store
        self.session_id = session_id
        self.window = window

    def load_context(self) -> list[Message]:
        """Messages récents formatés OpenAI, prêts à être injectés au LLM.

        Retourne au plus ``window`` messages ; si la fenêtre est pleine et que
        le premier message utilisateur de la session en a été évincé, il est
        réinjecté en tête (intention initiale)."""
        try:
            messages = self._store.get_messages(self.session_id, limit=self.window)
        except Exception as exc:  # dégradation gracieuse
            logger.warning("Mémoire short-term indisponible (%s) : contexte vide", exc)
            return []
        if not messages:
            return []
        if len(messages) >= self.window:
            # Sonde d'historique : retrouve le premier message utilisateur de
            # la session et le réinjecte s'il a été évincé de la fenêtre.
            try:
                history = self._store.get_messages(self.session_id, limit=1000)
            except Exception as exc:
                logger.warning("Sonde d'historique impossible (%s)", exc)
                history = messages
            first_user = next((m for m in history if m.get("role") == "user"), None)
            if first_user and all(m != first_user for m in messages):
                # Injection additive : fenêtre + message d'intention (jamais
                # d'éviction du contexte le plus récent).
                messages = [first_user, *messages]
        return self._to_openai(messages)

    def record(
        self, role: str, content: str, tool_calls: list[dict[str, Any]] | None = None
    ) -> bool:
        """Persiste un message dans la session (True si enregistré).

        Jamais bloquant : un échec du store est loggé et ignoré — le run
        agent doit pouvoir se poursuivre sans mémoire."""
        try:
            stored = self._store.append_message(self.session_id, role, content, tool_calls)
            return stored is not None
        except Exception as exc:
            logger.warning("Écriture mémoire short-term impossible (%s)", exc)
            return False

    def _to_openai(self, messages: list[dict[str, Any]]) -> list[Message]:
        """Projection vers le format OpenAI (role/content), champs store ignorés."""
        return [
            {"role": m.get("role", ""), "content": m.get("content", "")}
            for m in messages
            if m.get("role") and m.get("content") is not None
        ]
