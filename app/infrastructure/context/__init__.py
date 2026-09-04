"""Adaptateurs du port ``ContextPort`` (optimisation du contexte).

Outre le wrapper strangler vers le module legacy ``ia/agent/context.py``
(SQL libre de toute dépendance du noyau v2), on fournit un adaptateur
**déterministe** (``NullContextProvider``) : un faux fidèle qui ne résume
jamais ni ne tronque — utile pour les tests de use-cases hors ligne et pour
le profil ``AGENT_CONTEXT=0``.
"""

from __future__ import annotations

from app.domain.ports import ContextPort
from app.infrastructure.context.legacy_context import LegacyContextProvider
from app.infrastructure.context.null_context import NullContextProvider

__all__ = [
    "ContextPort",
    "LegacyContextProvider",
    "NullContextProvider",
    "default_context_provider",
]


def default_context_provider(context_enabled: bool = True) -> ContextPort:
    """Adaptateur par défaut.

    - ``context_enabled=True`` → wrapper legacy (fonctionnel, identique au v1) ;
    - ``context_enabled=False`` → ``NullContextProvider`` (aucune I/O, aucune
      mutation de l'historique) pour le profil ``AGENT_CONTEXT=0``.
    """
    return LegacyContextProvider() if context_enabled else NullContextProvider()
