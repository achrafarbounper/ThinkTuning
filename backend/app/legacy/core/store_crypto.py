# project/core/store_crypto.py

"""Chiffrement au repos des stores (P2 durable, lot 16) — sessions & audit.

Les contenus conversationnels (``content`` / ``thinking`` /
``tool_calls_json`` de ``agent_session_messages``) et le détail d'audit
(``detail_json`` d'``agent_audit``) sont chiffrés à l'écriture quand une clé
est configurée :

    - ``STORE_ENCRYPTION_KEY`` : base64 urlsafe d'une clé Fernet 32 octets.
      Génération : ``python -c "from cryptography.fernet import Fernet;
      print(Fernet.generate_key().decode())"`` ;
    - clé absente  -> mode PASSTHROUGH (comportement historique, warning
      au premier appel en production) ;
    - clé présente -> écriture chiffrée (préfixe ``enc:`` + token Fernet),
      lecture déchiffrée transparente (les anciennes lignes en clair —
      préfixe absent — restent lisibles, zéro migration).

Garanties :
    - fail-closed : en production (``is_production_env``), lancer l'API SANS
      clé de chiffrement est refusé (voir ``ensure_store_crypto_configured``,
      appelé au démarrage) — pas de données à risque écrites en clair ;
    - format auto-descriptif : le préfixe ``enc:`` permet de distinguer les
      lignes chiffrées des lignes legacy en clair ;
    - jamais de secret en double : la clé vit dans le vault/env de
      l'opérateur, pas dans la base.

Dépendance : ``cryptography`` (Fernet — AES-128-CBC + HMAC-SHA256, norme
libsodium). Ajoutée à ``requirements.txt`` (P2 lot 15/16).
"""

from __future__ import annotations

import base64
import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

# Marqueur de préfixe pour les valeurs chiffrées (absence = legacy en clair).
ENC_PREFIX = "enc:"

# Nom de la variable de clé (documentée dans .env.example).
ENV_KEY_NAME = "STORE_ENCRYPTION_KEY"


class StoreCryptoError(RuntimeError):
    """Clé invalide / échec de déchiffrement (donnée illisible)."""


@lru_cache(maxsize=4)
def _fernet_for(key_b64: str):
    """Instance Fernet mise en cache (coûteuse à construire)."""
    from cryptography.fernet import Fernet, InvalidToken

    try:
        return Fernet(key_b64), InvalidToken
    except (ValueError, TypeError) as exc:
        raise StoreCryptoError(
            f"{ENV_KEY_NAME} invalide : {exc} (générez : python -c "
            '"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")'
        ) from exc


def _load_key() -> str | None:
    """Retourne la clé Fernet (base64 urlsafe) ou None (pas de chiffrement)."""
    raw = (os.getenv(ENV_KEY_NAME) or "").strip()
    if not raw:
        return None
    # Tolérance : accepte la chaîne déjà base64url-safe (32 octets) OU brute.
    try:
        base64.urlsafe_b64decode(raw)
        return raw
    except (ValueError, TypeError):
        # Clé brute 32 octets -> encodée en base64url.
        try:
            return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")
        except Exception as exc:  # pragma: no cover - défensif
            raise StoreCryptoError(f"{ENV_KEY_NAME} illisible") from exc


def is_crypto_enabled() -> bool:
    """True si une clé de chiffrement est configurée (mode actif)."""
    return _load_key() is not None


def ensure_store_crypto_configured() -> None:
    """Fail-closed production : refuse le démarrage sans clé de chiffrement.

    En dev/test, une clé absente est tolérée (warning) — pas de comportement
    bloquant pour les tests ni le développement local.
    """
    from app.infrastructure.security.api_key import is_production_env

    if is_production_env() and not is_crypto_enabled():
        raise RuntimeError(
            f"{ENV_KEY_NAME} absente en production : les stores sessions/audit "
            "contenant des données personnelles doivent être chiffrés au repos. "
            "Générez une clé : python -c \"from cryptography.fernet import "
            'Fernet; print(Fernet.generate_key().decode())".'
        )
    if not is_crypto_enabled():
        logger.warning(
            "STORE_ENCRYPTION_KEY absente : sessions/audit stockés EN CLAIR "
            "(chiffrement au repos désactivé — recommandé en production)."
        )


def encrypt_text(plaintext: str) -> str:
    """Chiffre une valeur (préfixe ``enc:``). Passthrough si pas de clé."""
    if not is_crypto_enabled():
        return plaintext
    key = _load_key()
    if key is None:  # race théorique (env changé entre les deux appels)
        return plaintext
    fernet, _ = _fernet_for(key)
    token = fernet.encrypt(str(plaintext or "").encode("utf-8"))
    return ENC_PREFIX + token.decode("ascii")


def decrypt_text(value: str) -> str:
    """Déchiffre une valeur préfixée ``enc:``. Les valeurs en clair (legacy,
    ou mode passthrough) sont retournées telles quelles."""
    if not value or not value.startswith(ENC_PREFIX):
        return value
    if not is_crypto_enabled():
        # Donnée chiffrée mais clé disparue : on ne peut PAS la lire.
        raise StoreCryptoError(
            f"donnée chiffrée ({ENV_KEY_NAME}) sans clé configurée — lecture impossible"
        )
    key = _load_key()
    if key is None:  # pragma: no cover - garde (cf. ci-dessus)
        raise StoreCryptoError("clé de chiffrement absente")
    fernet, invalid_token = _fernet_for(key)
    try:
        return fernet.decrypt(value[len(ENC_PREFIX) :].encode("ascii")).decode("utf-8")
    except invalid_token as exc:
        raise StoreCryptoError("échec de déchiffrement (clé changée ou donnée corrompue)") from exc


def encrypt_json_like(value: str) -> str:
    """Helper pour les colonnes JSON (on chiffre la chaîne JSON complète)."""
    return encrypt_text(value)


__all__ = [
    "ENC_PREFIX",
    "ENV_KEY_NAME",
    "StoreCryptoError",
    "decrypt_text",
    "encrypt_json_like",
    "encrypt_text",
    "ensure_store_crypto_configured",
    "is_crypto_enabled",
]
