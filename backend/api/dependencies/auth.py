# project/api/dependencies/auth.py

from fastapi import Header, HTTPException

from app.infrastructure.security.api_key import (
    effective_api_key,
    is_valid_api_key,
)


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
    """Avertit (une fois au démarrage) si la clé API de développement est active.

    P0 SEC (F1) : en production (ENV/APP_ENV/THINKTUNING_ENV=prod) une clé
    absente ou faible fait ÉCHOUER le démarrage (fail-closed) au lieu d'un
    simple warning — délégation à ``ensure_api_key_configured()``.
    """
    from app.infrastructure.security.api_key import ensure_api_key_configured

    ensure_api_key_configured()


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> bool:
    # Comparaison à temps constant déléguée au module partagé (le transport
    # MCP SSE applique exactement la même vérification — P5).
    if not is_valid_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")
    return True
