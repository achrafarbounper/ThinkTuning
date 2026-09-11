"""JWT courte durée (P2 durable, lot 13) — implémentation DOMAINE, stdlib pure.

Tokens d'accès HS256 (RFC 7519) émis pour les **service accounts** (machine
to machine) et réutilisables comme creds alternative à ``X-API-Key`` sur la
surface REST / WebSocket :

    - ``iss``  : "thinktuning" (émetteur figé) ;
    - ``sub``  : identifiant du service account (ou ``api:<hash>`` pour un
      jeton dérivé de la clé admin) ;
    - ``role`` : rôle canonique (``admin`` / ``read`` — aligné sur la
      hiérarchie existante ``API_KEY`` vs ``API_KEY_READ``) ;
    - ``scopes`` : liste de scopes explicites (least-privilege) ;
    - ``jti``  : identifiant unique (révocation côté store) ;
    - ``iat`` / ``exp`` : bornes temporelles strictes (durée COURTE,
      défaut 15 minutes — rotation continue, pas de long-lived token).

Choix techniques :
    - **stdlib uniquement** (hmac/hashlib/base64/json) : zéro dépendance
      ajoutée, même philosophie que ``app/config/settings.py``
      (``_load_dotenv_to_environ`` sans python-dotenv). ``pyjwt`` reste une
      alternative acceptable si l'écosystème l'exige un jour ;
    - les fonctions de signature acceptent le secret EN PARAMÈTRE (le
      domaine ne lit pas l'environnement) — l'adaptateur
      (``infrastructure/security/jwt.py``) le résout depuis le vault/env ;
    - comparaison HMAC à temps constant ; validation stricte des claims
      (exp, iat futur rejeté, iss, signature) — jamais de tolérance ``none``
      algorithm (HS256 uniquement).

Contexte ThinkTuning : la clé API statique (``API_KEY``, rotation récente)
reste l'arbitre historique. Les JWT ajoutent : expiration courte, révocation
par ``jti``, scopes par compte, et audit d'accès via le store de service
accounts. Convivence : un endpoint accepte les deux (dépendance
``require_api_key_or_jwt`` dans la couche ``api/``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal

# Rôles canoniques alignés sur la hiérarchie existante API_KEY (admin) vs
# API_KEY_READ (lecture seule) — cf. app/infrastructure/security/api_key.py.
TokenRole = Literal["admin", "read"]

DEFAULT_TTL_SECONDS = 900  # 15 minutes — courte durée par conception
MAX_TTL_SECONDS = 86400  # 24 h — plafond dur (pas de long-lived token)
ISSUER = "thinktuning"

_ALGO_NAME = "HS256"


class TokenError(ValueError):
    """Erreur de création/vérification d'un jeton (message actionnable)."""


class TokenExpiredError(TokenError):
    """Le jeton est expiré (``exp`` dans le passé)."""


class TokenInvalidError(TokenError):
    """Le jeton est invalide (signature, structure ou claims)."""


# ============================================================
# Codage base64url (RFC 4648 §5, sans padding)
# ============================================================


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, TypeError) as exc:
        raise TokenInvalidError("segment base64url mal formé") from exc


# ============================================================
# Signatures & claims
# ============================================================


def sign_payload(payload: dict[str, Any], secret: str) -> str:
    """Signe un payload (HMAC-SHA256) et retourne un JWT complet.

    Le secret doit être une chaîne non vide d'au moins 16 caractères
    (appelant = adaptateur : résolu depuis vault/env, jamais en dur).
    """
    if not isinstance(secret, str) or len(secret) < 16:
        raise TokenError("secret JWT trop court (< 16 caractères)")
    header = {"alg": _ALGO_NAME, "typ": "JWT"}
    encoded_header = _b64url_encode(
        json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    encoded_payload = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    signing_input = f"{encoded_header}.{encoded_payload}".encode()
    digest = hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()
    signature = _b64url_encode(digest)
    return f"{encoded_header}.{encoded_payload}.{signature}"


def create_access_token(
    *,
    subject: str,
    secret: str,
    role: TokenRole = "admin",
    scopes: list[str] | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    jti: str | None = None,
    issuer: str = ISSUER,
    now: float | None = None,
) -> str:
    """Émet un JWT d'accès à durée COURTE (défaut 15 min, plafond 24 h).

    Args:
        subject: identifiant du service account demandeur (``sub``).
        secret:  clé HMAC serveur (vault/env — jamais en dur).
        role:    rôle canonique (admin / read) — bornes d'accès.
        scopes:  scopes explicites additionnels (ex. ``["train:read"]``).
        ttl_seconds: durée de vie en secondes (bornée à ``MAX_TTL_SECONDS``).
        jti:     identifiant unique (défaut : uuid4 — révocation possible).
        issuer:  émetteur (défaut ``thinktuning`` — validation stricte).

    Le timestamp ``now`` est injectable (tests déterministes).
    """
    if not isinstance(subject, str) or not subject.strip():
        raise TokenError("subject (service account) requis")
    if role not in ("admin", "read"):
        raise TokenError(f"rôle inconnu : {role!r} (admin|read)")
    ttl = max(1, min(int(ttl_seconds), MAX_TTL_SECONDS))
    now_ts = int(now if now is not None else time.time())
    payload: dict[str, Any] = {
        "iss": issuer,
        "sub": subject,
        "role": role,
        "scopes": list(scopes or []),
        "jti": jti or secrets.token_urlsafe(18),
        "iat": now_ts,
        "exp": now_ts + ttl,
    }
    return sign_payload(payload, secret)


def _decode_segments(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    """Décode header/payload et retourne la signature brute (sans valider)."""
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenInvalidError("un JWT doit contenir 3 segments (header.payload.signature)")
    try:
        header = json.loads(_b64url_decode(parts[0]))
        payload = json.loads(_b64url_decode(parts[1]))
        signature = base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4))
    except (ValueError, TypeError) as exc:
        raise TokenInvalidError("segments JWT illisibles") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise TokenInvalidError("header/payload JWT non-objets")
    return header, payload, signature


def _encoded_signing_input(header: dict[str, Any], payload: dict[str, Any]) -> str:
    """Re-sérialise header/payload de façon CANONIQUE (sort_keys, compact)."""
    h = _b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    p = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    return f"{h}.{p}"


def verify_access_token(
    token: str,
    secret: str,
    *,
    issuer: str = ISSUER,
    now: float | None = None,
) -> dict[str, Any]:
    """Valide un JWT (signature HS256, exp, iat, iss) et retourne les claims.

    Lève ``TokenExpiredError`` / ``TokenInvalidError`` (messages actionnables).
    ``now`` injectable pour des tests déterministes.
    """
    header, payload, signature = _decode_segments(token)

    # Algorithme FIFFÉ : refus de tout ``alg`` autre que HS256 (y compris
    # ``none``) — anti-confusion d'algorithme.
    algo = header.get("alg")
    if algo != _ALGO_NAME:
        raise TokenInvalidError(f"algorithme refusé : {algo!r} (HS256 uniquement)")

    # Signature — comparaison à temps constant (HMAC de la string à signer).
    signing_input = _encoded_signing_input(header, payload)
    expected = hmac.new(
        secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256
    ).digest()
    if not hmac.compare_digest(expected, signature):
        raise TokenInvalidError("signature invalide (secret inconnu ou token altéré)")

    now_ts = int(now if now is not None else time.time())

    exp = payload.get("exp")
    if not isinstance(exp, int):
        raise TokenInvalidError("claim 'exp' manquant ou non numérique")
    if now_ts >= exp:
        raise TokenExpiredError(f"jeton expiré (exp={exp})")

    iat = payload.get("iat", 0)
    if isinstance(iat, int) and iat > now_ts + 300:
        # Tolérance d'horloge de 5 min pour le futur (légitime à l'émission).
        raise TokenInvalidError(f"jeton émis dans le futur (iat={iat})")

    if payload.get("iss") != issuer:
        raise TokenInvalidError(f"émetteur inconnu : {payload.get('iss')!r}")

    role = payload.get("role")
    if role not in ("admin", "read"):
        raise TokenInvalidError(f"rôle invalide dans le jeton : {role!r}")
    if not isinstance(payload.get("sub"), str) or not payload["sub"].strip():
        raise TokenInvalidError("claim 'sub' manquant ou invalide")
    if not isinstance(payload.get("scopes", []), list):
        raise TokenInvalidError("claim 'scopes' invalide")

    return payload


# ============================================================
# Helpers partagés (clés API -> secret de signature dérivé)
# ============================================================


def derive_signing_secret(*parts: str) -> str:
    """Dérive une clé HMAC 256 bits depuis une ou plusieurs sources.

    Utilisé par l'adaptateur pour dériver ``JWT_SECRET`` depuis ``API_KEY``
    quand aucune variable ``JWT_SECRET`` dédiée n'est posée : un attaquant
    qui lit la clé API ne peut PAS forger de token sans connaître cette
    dérivation (SHA-256, non réversible).
    """
    raw = "|".join(str(p) for p in parts if p)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def hash_principal(client_id: str) -> str:
    """Empreinte courte (16 chars) d'un principal pour l'audit (jamais le secret)."""
    return hashlib.sha256(str(client_id).encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class TokenClaims:
    """Claims d'un jeton validé — accès typé sans dépendre des dicts."""

    subject: str
    role: TokenRole
    scopes: tuple[str, ...] = field(default_factory=tuple)
    jti: str = ""
    issued_at: int = 0
    expires_at: int = 0

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> TokenClaims:
        return cls(
            subject=str(payload.get("sub", "")),
            role=payload.get("role", "read"),
            scopes=tuple(payload.get("scopes") or []),
            jti=str(payload.get("jti", "")),
            issued_at=int(payload.get("iat", 0)),
            expires_at=int(payload.get("exp", 0)),
        )

    @property
    def is_read_only(self) -> bool:
        return self.role == "read"


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "ISSUER",
    "MAX_TTL_SECONDS",
    "TokenClaims",
    "TokenError",
    "TokenExpiredError",
    "TokenInvalidError",
    "create_access_token",
    "derive_signing_secret",
    "hash_principal",
    "sign_payload",
    "verify_access_token",
]
