# project/api/dependencies/auth.py

import logging
import os
import secrets

from fastapi import Header, HTTPException

logger = logging.getLogger(__name__)

# Repli de développement : utilisé UNIQUEMENT si API_KEY n'est pas défini.
# Un warning est émis au démarrage (cf. api/main.py) pour qu'une exposition
# réseau avec cette clé publique ne passe jamais inaperçue.
_DEV_FALLBACK_KEY = "dev-local-api-key"


def _get_api_key() -> str:
    """Clé API effective, lue à chaque appel (source unique de vérité).

    La lecture à l'appel (et non à l'import) permet aux tests et aux
    processus longs de changer la clé via l'environnement sans recharger
    le module — et supprime la duplication qui existait avec api/__init__.py.
    """
    return os.getenv("API_KEY") or _DEV_FALLBACK_KEY


def warn_if_insecure_api_key() -> None:
    """Avertit (une fois au démarrage) si la clé API de développement est active."""
    if not os.getenv("API_KEY"):
        logger.warning(
            "API_KEY absente de l'environnement : la clé de développement par "
            "défaut est active. Définissez API_KEY avant toute exposition réseau."
        )


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> bool:
    expected_key = _get_api_key()
    # Comparaison à temps constant : `!=` fuiterait la clé octet par octet via
    # la mesure du temps de réponse (timing attack).
    if x_api_key is None or not secrets.compare_digest(x_api_key, expected_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")
    return True
