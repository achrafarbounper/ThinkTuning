# project/app/infrastructure/security/api_key.py

"""Primitives de clé API partagées — source unique de vérité (P5).

Utilisées par DEUX frontières :

    - la dépendance FastAPI de la surface REST (``api/dependencies/auth.py``) ;
    - le transport MCP HTTP (``POST /mcp/sse``), qui n'a pas le droit
      d'importer la couche ``api`` (règle hexagonale : les dépendances
      pointent vers l'intérieur) — d'où ce module en ``app/infrastructure``.

Convention identique aux deux frontières : ``API_KEY`` lue dans
l'environnement À CHAQUE APPEL (rotation / tests sans rechargement), repli
de développement signalé au démarrage (cf. ``api/main.py``), comparaison à
temps constant (timing attack).
"""

from __future__ import annotations

import os
import secrets

# Repli de développement : utilisé UNIQUEMENT si API_KEY n'est pas défini.
# Un warning est émis au démarrage pour qu'une exposition réseau avec cette
# clé publique ne passe jamais inaperçue.
DEV_FALLBACK_KEY = "dev-local-api-key"


def effective_api_key() -> str:
    """Clé API effective attendue (lecture à l'appel, pas à l'import)."""
    return os.getenv("API_KEY") or DEV_FALLBACK_KEY


def is_valid_api_key(candidate: str | None) -> bool:
    """Vrai si ``candidate`` correspond à la clé attendue (temps constant).

    ``None`` (en-tête absent) → ``False`` : fail-closed, même sémantique que
    l'ancien ``x_api_key is None or not compare_digest(...)`` de la surface
    REST.
    """
    if candidate is None:
        return False
    return secrets.compare_digest(candidate, effective_api_key())


__all__ = ["DEV_FALLBACK_KEY", "effective_api_key", "is_valid_api_key"]
