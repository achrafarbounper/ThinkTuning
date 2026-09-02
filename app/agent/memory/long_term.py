"""Mémoire long-term : résumés persistés par clé (key/value).

S'appuie sur ``SessionStorePort.save_memory/get_memory/delete_memory``
(cf. table memory de core/session_store.py). Rôle dans la boucle agentique :
persister ce qui survit à la fenêtre courte — préférences utilisateur,
conclusions validées, synthèses de runs antérieurs.

Conventions :
    - clé = identifiant stable (ex. ``session:<id>:summary``,
      ``user:<id>:preferences``) ; le namespace est de la responsabilité de
      l'appelant, ce module ne fait aucune transformation ;
    - lecture tolérante (clé absente -> chaîne vide, convention du store
      legacy) ; écriture/effacement loggés en DEBUG pour traçabilité.
"""

from __future__ import annotations

import logging

from app.domain.ports import SessionStorePort

logger = logging.getLogger("thinktuning.agent.memory")


class LongTermMemory:
    """Résumés persistants au-dessus du store de sessions."""

    def __init__(self, store: SessionStorePort):
        self._store = store

    def remember(self, key: str, summary: str) -> None:
        """Persiste (ou remplace) un résumé pour une clé."""
        self._store.save_memory(key, summary)
        logger.debug("Mémoire long-term : %s (%d caractères)", key, len(summary))

    def recall(self, key: str) -> str:
        """Renvoie le résumé associé à la clé (chaîne vide si absent)."""
        return self._store.get_memory(key)

    def forget(self, key: str) -> None:
        """Efface un résumé (idempotent : clé absente = no-op)."""
        self._store.delete_memory(key)
        logger.debug("Mémoire long-term : effacement %s", key)
