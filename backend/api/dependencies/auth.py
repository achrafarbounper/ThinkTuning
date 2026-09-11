# project/api/dependencies/auth.py

from fastapi import Header, HTTPException, Request

from app.domain.tokens import (
    TokenClaims,
    TokenExpiredError,
    TokenInvalidError,
    derive_signing_secret,
    verify_access_token,
)
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


# ============================================================================
# JWT courte durée (P2 durable, lot 13)
# ============================================================================


def jwt_secret() -> str:
    """Clé de signature HS256 : ``JWT_SECRET`` (vault/env) sinon dérivée de
    la clé API (SHA-256 — non réversible, cf. ``derive_signing_secret``).

    P2 lot 13 (vault) : la lecture passe PAR LE VAULT (``get_vault_secret``,
    audit d'accès inclus — action ``secret_access``) quand un backend est
    configuré (``VAULT_BACKEND=doppler|infisical|azure``) ; en mode ``env``
    (défaut), le vault lit l'environnement : comportement inchangé, plus
    d'audit. La dérivation garantit un repli sûr sans nouvelle variable.
    """
    import os

    try:
        from app.infrastructure.security.vault import get_vault_secret

        from_vault = get_vault_secret("JWT_SECRET")
        if from_vault and from_vault.strip():
            return from_vault.strip()
    except Exception:  # noqa: BLE001 - vault indisponible -> repli env/dérivé
        pass
    dedicated = os.getenv("JWT_SECRET", "").strip()
    if dedicated:
        return dedicated
    return derive_signing_secret(effective_api_key(), "thinktuning-jwt")


def authenticate_bearer_token(authorization: str) -> TokenClaims:
    """Valide un ``Authorization: Bearer <jwt>`` (révocation incluse).

    Lève ``HTTPException(401)`` avec un détail actionnable. Le jti est
    vérifié contre la liste de révocation (service_accounts).
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="En-tête Authorization: Bearer requis.")
    token = authorization[len("Bearer ") :].strip()
    try:
        payload = verify_access_token(token, jwt_secret())
    except TokenExpiredError as exc:
        raise HTTPException(status_code=401, detail=f"Jeton expiré : {exc}") from exc
    except TokenInvalidError as exc:
        raise HTTPException(status_code=401, detail=f"Jeton invalide : {exc}") from exc
    claims = TokenClaims.from_payload(payload)
    from app.infrastructure.security.service_accounts import get_service_account_store

    if get_service_account_store().is_token_revoked(claims.jti):
        raise HTTPException(status_code=401, detail="Jeton révoqué.")
    return claims


def resolve_principal(request: Request) -> dict | None:
    """Principal authentifié pour l'audit (None si non authentifié)."""
    return getattr(request.state, "auth_principal", None)


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


# --- Variantes bipolaires (API_KEY OU JWT) — P2 lot 13 -----------------------


def _require_api_key_or_jwt(
    request: Request,
    x_api_key: str | None,
    authorization: str | None,
    *,
    read_scope: bool = False,
) -> bool:
    """Cœur partagé : X-API-Key (historique) OU Bearer JWT (nouveau).

    - ``read_scope=True`` : clé ``API_KEY_READ`` OU jeton rôle ``read``
      (least-privilege — le monitoring ne peut pas ouvrir les routes admin) ;
    - l'identité résolue est posée sur ``request.state.auth_principal``
      (consommée par l'audit / les middlewares) ;
    - ordre de priorité : Bearer JWT d'abord (explicite), puis X-API-Key —
      un header invalide ne fait PAS échouer l'autre voie (défensif).
    """
    if authorization and authorization.startswith("Bearer "):
        claims = authenticate_bearer_token(authorization)
        if read_scope and claims.role != "read":
            raise HTTPException(
                status_code=403,
                detail="Jeton sans le rôle lecture (read) requis pour cette route.",
            )
        request.state.auth_principal = {
            "type": "jwt",
            "subject": claims.subject,
            "role": claims.role,
            "jti": claims.jti,
        }
        return True
    if read_scope:
        if is_valid_read_api_key(x_api_key):
            request.state.auth_principal = {"type": "api_key", "subject": "api-key", "role": "read"}
            return True
    elif is_valid_api_key(x_api_key):
        request.state.auth_principal = {"type": "api_key", "subject": "api-key", "role": "admin"}
        return True
    raise HTTPException(
        status_code=401,
        detail="Authentification requise (X-API-Key ou Bearer JWT).",
    )


def require_api_key_or_jwt(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> bool:
    """Admin : X-API-Key OU Bearer JWT (rôle read exclu des routes d'action)."""
    return _require_api_key_or_jwt(request, x_api_key, authorization, read_scope=False)


def require_read_api_key_or_jwt(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
) -> bool:
    """Lecture seule : X-API-Key (admin/read) OU Bearer JWT rôle read."""
    return _require_api_key_or_jwt(request, x_api_key, authorization, read_scope=True)


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
    if query_key:
        if read_scope and is_valid_read_api_key(query_key):
            return True
        if is_valid_api_key(query_key):
            return True
        # P2 lot 13 : jeton JWT (query) — courte durée, révocation, rôle.
        try:
            claims = TokenClaims.from_payload(verify_access_token(query_key, jwt_secret()))
        except (TokenExpiredError, TokenInvalidError):
            return False
        from app.infrastructure.security.service_accounts import get_service_account_store

        if get_service_account_store().is_token_revoked(claims.jti):
            return False
        if read_scope and claims.role != "read":
            return False
        return True
    return False
