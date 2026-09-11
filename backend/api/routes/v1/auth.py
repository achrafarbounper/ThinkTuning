# project/api/routes/v1/auth.py
"""Endpoints d'authentification moderne (P2 durable, lot 13).

    POST   /api/v1/auth/token                         client credentials -> JWT courte durée
    GET    /api/v1/auth/verify                        valide le jeton présenté (claims)
    POST   /api/v1/auth/revoke                        révoque le jti courant
    POST   /api/v1/auth/service-accounts              crée un service account (admin)
    GET    /api/v1/auth/service-accounts              liste les comptes (admin)
    DELETE /api/v1/auth/service-accounts/{account_id} révoque un compte (admin)

Conventions :
    - ``POST /auth/token`` est le point d'échange PUBLIC (c'est LUI qui
      authentifie) : corps JSON ``{client_id, client_secret, ttl_seconds?}`` ;
    - les routes de gestion de comptes exigent la clé ADMIN (``require_api_key``) :
      un service account ne peut pas créer/voir/révoquer d'autres comptes ;
    - ``/verify`` répond 200 avec les claims (ou 401) — utilisé par le
      dashboard / les clients pour tester un jeton sans refaire l'échange ;
    - ``/revoke`` révoque le jti du jeton présenté (short-TTL + jti =
      révocation immédiate pour la durée restante) ;
    - chaque opération est auditée par le store (actions
      ``service_account_*``), jamais la valeur du secret.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from api.dependencies.auth import (
    authenticate_bearer_token,
    jwt_secret,
    require_api_key,
)
from app.infrastructure.security.service_accounts import (
    DEFAULT_TOKEN_TTL_SECONDS,
    MAX_TOKEN_TTL_SECONDS,
    get_service_account_store,
)

router = APIRouter(prefix="/auth", tags=["Auth v1"])


class TokenRequest(BaseModel):
    """Client credentials (RFC 6749 §4.3 — grant implicite de ce service)."""

    client_id: str = Field(min_length=1, description="Identifiant du service account")
    client_secret: str = Field(min_length=1, description="Secret du service account")
    ttl_seconds: int | None = Field(
        default=None, ge=60, le=MAX_TOKEN_TTL_SECONDS,
        description="Durée de vie du jeton (défaut 900 s, plafond 24 h)",
    )


class TokenResponse(BaseModel):
    token: str
    token_type: str = "Bearer"
    expires_in: int
    role: str


class ServiceAccountCreate(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    role: str = Field(default="read", pattern="^(admin|read)$")
    scopes: list[str] = Field(default_factory=list, max_length=32)


@router.post("/token", response_model=TokenResponse)
def token_endpoint(req: TokenRequest) -> TokenResponse:
    """Échange client_id + client_secret contre un JWT courte durée."""
    ttl = req.ttl_seconds if req.ttl_seconds is not None else DEFAULT_TOKEN_TTL_SECONDS
    try:
        issued = get_service_account_store().issue_token(
            client_id=req.client_id,
            client_secret=req.client_secret,
            jwt_secret=jwt_secret(),
            ttl_seconds=ttl,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return TokenResponse(
        token=issued["token"],
        expires_in=ttl,
        role=issued["role"],
    )


@router.get("/verify")
def verify_endpoint(request: Request) -> dict:
    """Valide le jeton porté par ``Authorization: Bearer`` et renvoie les claims.

    Le rôle ``read`` est accepté ici (vérification d'identité uniquement).
    """
    authorization = request.headers.get("Authorization")
    claims = authenticate_bearer_token(authorization or "")
    return {
        "valid": True,
        "subject": claims.subject,
        "role": claims.role,
        "scopes": list(claims.scopes),
        "jti": claims.jti,
        "expires_at": claims.expires_at,
    }


@router.post("/revoke", status_code=204, response_model=None)
def revoke_endpoint(request: Request) -> None:
    """Révoque le jti du jeton présenté (les 15 min restantes sont annulées)."""
    authorization = request.headers.get("Authorization")
    claims = authenticate_bearer_token(authorization or "")
    get_service_account_store().revoke_token(claims)


# --- Gestion des service accounts (ADMIN uniquement) --------------------------


@router.post("/service-accounts", status_code=201)
def create_service_account(
    req: ServiceAccountCreate,
    _: bool = Depends(require_api_key),
) -> dict:
    """Crée un service account — le secret en clair n'est renvoyé QU'ICI."""
    try:
        return get_service_account_store().create_account(
            name=req.name, role=req.role, scopes=req.scopes
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/service-accounts")
def list_service_accounts(_: bool = Depends(require_api_key)) -> dict:
    """Liste des comptes (jamais de hash/secret exposé)."""
    return {"items": get_service_account_store().list_accounts()}


@router.delete("/service-accounts/{account_id}", status_code=204, response_model=None)
def revoke_service_account(account_id: str, _: bool = Depends(require_api_key)) -> None:
    """Révoque un compte (suppression immédiate — les jetons en vie expirent au pire)."""
    if not get_service_account_store().revoke_account(account_id):
        raise HTTPException(status_code=404, detail="service account introuvable")
