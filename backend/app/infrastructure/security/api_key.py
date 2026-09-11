# project/app/infrastructure/security/api_key.py

"""Primitives de clé API partagées — source unique de vérité (P5 + P0 SEC).

Utilisées par DEUX frontières :

    - la dépendance FastAPI de la surface REST (``api/dependencies/auth.py``) ;
    - le transport MCP HTTP (``POST /mcp/sse``), qui n'a pas le droit
      d'importer la couche ``api`` (règle hexagonale : les dépendances
      pointent vers l'intérieur) — d'où ce module en ``app/infrastructure``.

Convention identique aux deux frontières : ``API_KEY`` lue dans
l'environnement À CHAQUE APPEL (rotation / tests sans rechargement), repli
de développement signalé au démarrage (cf. ``api/main.py``), comparaison à
temps constant (timing attack).

Durcissement P0 (docs/SECURITY_DIAGNOSTIC.md F1) : fail-closed en prod —
``ensure_api_key_configured()`` refuse le démarrage si ``ENV=prod`` (ou
``APP_ENV``/``THINKTUNING_ENV``) et que ``API_KEY`` est absente ou trop
courte (< 32 caractères). En dev/test le repli ``dev-local-api-key`` reste
actif pour ne pas casser les tests ni le démarrage local.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Repli de développement : utilisé UNIQUEMENT si API_KEY n'est pas défini
# ET hors production (cf. ensure_api_key_configured / is_production_env).
# Un warning est émis au démarrage pour qu'une exposition réseau avec cette
# clé publique ne passe jamais inaperçue.
DEV_FALLBACK_KEY = "dev-local-api-key"

# Longueur minimale exigée en production (256 bits ≈ 32 octets ≈ 43 chars
# base64-url via secrets.token_urlsafe(32) ; on exige >= 32 chars).
MIN_PROD_API_KEY_LENGTH = 32

# Longueur minimale de la clé de LECTURE en production (least-privilege : un
# jeton de monitoring/CI n'a pas besoin d'être aussi fort que l'admin).
MIN_PROD_READ_API_KEY_LENGTH = 16


def is_production_env() -> bool:
    """True si l'environnement déclare la production (ENV/APP_ENV/THINKTUNING_ENV=prod)."""
    for var in ("ENV", "APP_ENV", "THINKTUNING_ENV"):
        if os.getenv(var, "").strip().lower() in {"prod", "production"}:
            return True
    return False


def effective_api_key() -> str:
    """Clé API effective attendue (lecture à l'appel, pas à l'import)."""
    return os.getenv("API_KEY") or DEV_FALLBACK_KEY


def old_key_expired() -> bool:
    """True si API_KEY_OLD_EXPIRES (ISO 8601) est définie et PASSÉE.

    Fail-closed : une date absente = pas d'expiration ; une date INVALIDE est
    traitée comme expirée (la clé ancienne cesse d'être acceptée).
    """
    raw = os.getenv("API_KEY_OLD_EXPIRES", "").strip()
    if not raw:
        return False
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
    except ValueError:
        logger.warning(
            "API_KEY_OLD_EXPIRES invalide ('%s') : clé ancienne traitée comme expirée.",
            raw,
        )
        return True
    return datetime.now(UTC) >= expires


def legacy_api_key_active() -> bool:
    """True si la rotation est en cours (API_KEY_OLD définie et non expirée)."""
    if not os.getenv("API_KEY_OLD"):
        return False
    return not old_key_expired()


def _accepted_admin_keys() -> list[str]:
    """Clés acceptées sur les routes ADMIN : API_KEY + API_KEY_OLD (grace)."""
    keys = [effective_api_key()]
    if legacy_api_key_active():
        keys.append(os.getenv("API_KEY_OLD", ""))
    return [key for key in keys if key]


def _accepted_read_keys() -> list[str]:
    """Clés acceptées sur les routes LECTURE : admin (+ old) + API_KEY_READ."""
    keys = _accepted_admin_keys()
    read = os.getenv("API_KEY_READ")
    if read:
        keys.append(read)
    return keys


def is_valid_api_key(candidate: str | None) -> bool:
    """Vrai si ``candidate`` est une clé ADMIN acceptée (temps constant).

    ``None`` (en-tête absent) → ``False`` : fail-closed, même sémantique que
    l'ancien ``x_api_key is None or not compare_digest(...)`` de la surface
    REST. P1 (rotation) : la clé ANCIENNE (API_KEY_OLD) reste acceptée
    pendant la grâce — les clients en migration ne sont pas verrouillés.
    """
    if candidate is None:
        return False
    return any(secrets.compare_digest(candidate, expected) for expected in _accepted_admin_keys())


def is_valid_read_api_key(candidate: str | None) -> bool:
    """Vrai si ``candidate`` est une clé LECTURE acceptée (temps constant).

    Least-privilege P1 : accepte la clé admin (et l'ancienne en rotation) ET
    la clé dédiée ``API_KEY_READ``. Une clé read NE peut PAS ouvrir les
    routes admin (``is_valid_api_key`` ne l'accepte pas).
    """
    if candidate is None:
        return False
    return any(secrets.compare_digest(candidate, expected) for expected in _accepted_read_keys())


def generate_api_key(nbytes: int = 32) -> str:
    """Génère une clé API 256-bit (setup prod : ``python -c ...``)."""
    return secrets.token_urlsafe(nbytes)


def ensure_api_key_configured() -> str:
    """Fail-closed P0 : refuse une config prod sans clé forte.

    - prod (``is_production_env()``) + ``API_KEY`` absente → ``RuntimeError`` ;
    - prod + ``API_KEY`` < 32 chars → ``RuntimeError`` (fallback
      ``dev-local-api-key`` et placeholders type ``change-me`` rejetés) ;
    - dev/test → retourne ``effective_api_key()`` (repli dev autorisé).

    Appelé au démarrage (``api/main.py::lifespan``) : l'API ne démarre PAS
    en prod sans secret réel. Ne lève JAMAIS en dev/test (compat tests).

    P1 (rotation + clé lecture) :
        - prod + API_KEY_READ < 16 chars → RuntimeError (least-privilege) ;
        - prod + API_KEY_OLD configurée MAIS API_KEY_OLD_EXPIRES déjà passée
          → RuntimeError (rotation terminée et encore en place = fail-closed).
    """
    raw = (os.getenv("API_KEY") or "").strip()
    if is_production_env():
        if not raw:
            raise RuntimeError(
                "API_KEY absente en production (ENV=prod) : démarrage refusé "
                "(fail-closed, cf. docs/SECURITY_DIAGNOSTIC.md F1). "
                "Générez-en une : python -c "
                '"import secrets; print(secrets.token_urlsafe(32))".'
            )
        if len(raw) < MIN_PROD_API_KEY_LENGTH or raw in {"change-me", DEV_FALLBACK_KEY}:
            raise RuntimeError(
                f"API_KEY de production trop faible ({len(raw)} chars < "
                f"{MIN_PROD_API_KEY_LENGTH}) : démarrage refusé. "
                "Générez une clé 256-bit : python -c "
                '"import secrets; print(secrets.token_urlsafe(32))".'
            )
        read_raw = (os.getenv("API_KEY_READ") or "").strip()
        if read_raw and (
            len(read_raw) < MIN_PROD_READ_API_KEY_LENGTH
            or read_raw in {"change-me", DEV_FALLBACK_KEY}
        ):
            raise RuntimeError(
                f"API_KEY_READ de production trop faible ({len(read_raw)} chars < "
                f"{MIN_PROD_READ_API_KEY_LENGTH}) : démarrage refusé. "
                "Générez-en une : python -c "
                '"import secrets; print(secrets.token_urlsafe(24))".'
            )
        if os.getenv("API_KEY_OLD") and old_key_expired():
            raise RuntimeError(
                "API_KEY_OLD_EXPIRES est déjà passée : la rotation est "
                "ACHEVÉE mais API_KEY_OLD est encore configurée. Supprimez "
                "API_KEY_OLD/API_KEY_OLD_EXPIRES (ou repoussez la date) avant "
                "de démarrer (fail-closed)."
            )
        if os.getenv("API_KEY_OLD"):
            logger.warning(
                "Rotation en cours : API_KEY_OLD acceptée jusqu'à %s (API_KEY_OLD_EXPIRES).",
                os.getenv("API_KEY_OLD_EXPIRES") or "(sans date)",
            )
        return raw
    if not raw:
        logger.warning(
            "API_KEY absente : clé de développement par défaut active. "
            "Définissez API_KEY avant toute exposition réseau."
        )
    return raw or DEV_FALLBACK_KEY


__all__ = [
    "DEV_FALLBACK_KEY",
    "MIN_PROD_API_KEY_LENGTH",
    "MIN_PROD_READ_API_KEY_LENGTH",
    "effective_api_key",
    "ensure_api_key_configured",
    "generate_api_key",
    "is_production_env",
    "is_valid_api_key",
    "is_valid_read_api_key",
    "legacy_api_key_active",
    "old_key_expired",
]
