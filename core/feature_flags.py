# project/core/feature_flags.py

"""Feuilles de route (« feature flags ») des enhancements de l'agent.

Toutes les fonctionnalités nouvelles de l'agent (fiabilité, audit,
analytique d'outils, gestion du contexte, copilot, websocket) sont livrées
DÉSACTIVÉES par défaut : tant qu'un flag n'est pas explicitement activé, le
comportement historique est strictement préservé (principe de rollout
incrémental, décision du programme « AI Agent Enhancement »).

Chaque flag est piloté par une variable d'environnement ``AGENT_<NOM>`` dont
les valeurs « 1 / true / yes / on » (insensibles à la casse) signifient activé ;
toute autre valeur, y compris l'absence de la variable, signifie désactivé.

Référencé par l'agent et l'API via ``flag(name)`` / ``features()``. Les flags
sont relus à chaque appel (pas de cache) : un test peut basculer un flag par
``monkeypatch.setenv`` puis re-lire immédiatement, sans rechargement manuel.
"""

import os

# Noms canoniques des flags portés par le programme. Chaque entrée correspond à
# une Phase des enhancements :
#   reliability    (A) retry + circuit breaker + classification des erreurs LLM
#   audit          (A) journal d'audit / conformité
#   tool_analytics (B) découverte, recommandation et analytique d'outils
#   context        (C) gestion du contexte (résumé, fenêtre, mémoire)
#   copilot        (D) suggestions de type « GitHub Copilot »
#   websocket      (E) canal bidirectionnel WebSocket
#   multi_agent    (F) orchestration multi-agents (superviseur/workers)
FEATURES = (
    "reliability",
    "audit",
    "tool_analytics",
    "context",
    "copilot",
    "websocket",
    "multi_agent",
)

# Valeurs considérées comme « activé » (insensible à la casse).
_TRUE_VALUES = {"1", "true", "yes", "on"}


def flag(name: str) -> bool:
    """État d'un flag (False si inconnu ou non activé)."""
    if name not in FEATURES:
        return False
    value = os.getenv(f"AGENT_{name.upper()}", "").strip().lower()
    return value in _TRUE_VALUES


def features() -> dict[str, bool]:
    """Snapshot de tous les flags — pour les logs / la config exposée."""
    return {name: flag(name) for name in FEATURES}


def active_features() -> list[str]:
    """Liste ordonnée des flags activés (utile en tête de log / statut)."""
    return [name for name in FEATURES if flag(name)]