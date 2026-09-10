# project/api/dependencies/auth.py

import logging
import os

from fastapi import Header, HTTPException

from app.infrastructure.security.api_key import (
    effective_api_key,
    is_valid_api_key,
)

logger = logging.getLogger(__name__)


def _get_api_key() -> str:
    """Clé API effective (source unique : app/infrastructure/security/api_key).

    La lecture à l'appel (et non à l'import) permet aux tests et aux
    processus longs de changer la clé via l'environnement sans recharger
    le module — et supprime la duplication qui existait avec api/__init__.py.
    Le même module sert au transport MCP SSE (P5), qui ne peut pas importer
    la couche ``api`` (règle hexagonale).
    """
    return effective_api_key()


def warn_if_insecure_api_key() -> None:
    """Avertit (une fois au démarrage) si la clé API de développement est active."""
    if not os.getenv("API_KEY"):
        logger.warning(
            "API_KEY absente de l'environnement : la clé de développement par "
            "défaut est active. Définissez API_KEY avant toute exposition réseau."
        )


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> bool:
    # Comparaison à temps constant déléguée au module partagé (le transport
    # MCP SSE applique exactement la même vérification — P5).
    if not is_valid_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")
    return True
