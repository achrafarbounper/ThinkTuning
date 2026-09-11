# project/api/dependencies/auth.py

from fastapi import Header, HTTPException

from app.infrastructure.security.api_key import (
    effective_api_key,
    is_valid_api_key,
    is_valid_read_api_key,
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


def require_read_api_key(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> bool:
    """Dépendance LECTURE (least-privilege, P1 point 10a).

    Accepte ``API_KEY_READ`` (clé dédiée monitoring/CI) OU la clé admin
    (et l'ancienne pendant la rotation). Une clé read ne peut PAS ouvrir
    les routes admin : ``require_api_key`` ne la reconnaît pas.
    """
    if not is_valid_read_api_key(x_api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")
    return True


def ws_is_authorized(websocket, *, read_scope: bool = False) -> bool:
    """Auth d'un WebSocket (P1 point 10a — header d'abord, query en repli).

    Les navigateurs ne peuvent PAS poser d'en-tête sur un WebSocket natif :
    l'en-tête ``X-API-Key`` est accepté EN PREMIER (clients non navigateur —
    évite le jeton dans les logs d'accès), puis le query param ``?token=``
    (repli dashboard). Vérification déléguée aux mêmes fonctions à temps
    constant que le REST (rotation API_KEY_OLD incluse).

    ``read_scope=True`` : canal en lecture seule (métriques) — la clé
    ``API_KEY_READ`` suffit ; les canaux d'action (agent ws) restent admin.
    """
    header_key = websocket.headers.get("x-api-key")
    if header_key:
        if read_scope and is_valid_read_api_key(header_key):
            return True
        if is_valid_api_key(header_key):
            return True
    query_key = websocket.query_params.get("token")
    if read_scope and query_key is not None:
        return is_valid_read_api_key(query_key)
    return is_valid_api_key(query_key)
