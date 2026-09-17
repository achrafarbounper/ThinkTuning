# project/app/infrastructure/mcp/catalog_pagination.py
"""Pagination par curseur OPAQUE des catalogues MCP (v2.3.0).

Concerne ``tools/list``, ``resources/list`` et ``prompts/list`` (spec MCP :
``ListToolsResult`` / ``ListResourcesResult`` / ``ListPromptsResult``
supportent un champ optionnel ``nextCursor`` — pagination requête/réponse via
``params.cursor``).

Contrats :
    - curseur OPAQUE : le client ne décode PAS le curseur, il le renvoie tel
      quel. Ici : payload JSON compact (version, kind, offset, émission)
      encodé base64url + signature HMAC-SHA256 tronquée — infalsifiable et
      illisible hors serveur ;
    - curseur lié au CATALOGUE (``kind``) : un curseur obtenu sur
      ``tools/list`` est rejeté s'il est rejoué sur ``resources/list`` ;
    - EXPIRATION : TTL borné (``MCP_PAGINATION_CURSOR_TTL_SECONDS``, défaut
      900 s) — un curseur trop ancien est rejeté comme invalide (le client
      repart d'une liste sans curseur) ;
    - COMPATIBILITÉ : aucune pagination imposée au port — le serveur découpe
      la liste rendue par ``list_tools/list_resources/list_prompts``. Sans
      ``cursor`` dans ``params``, la PREMIÈRE page est rendue ; avec une taille
      de page par défaut (50) supérieure aux catalogues actuels, la réponse
      est identique aux versions 2.2.x (aucun ``nextCursor``).

Configurable :
    - ``MCP_PAGINATION_PAGE_SIZE``  : taille max de page (défaut 50, min 1) ;
    - ``MCP_PAGINATION_CURSOR_TTL_SECONDS`` : TTL des curseurs (défaut 900) ;
    - ``MCP_PAGINATION_SECRET`` : secret HMAC (défaut : secret aléatoire par
      process — les curseurs ne survivent pas à un redémarrage, accepté).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

# Version du format de curseur : invalide tout curseur d'une ancienne
# disposition du payload (défense en profondeur, le HMAC couvre déjà ceci).
_CURSOR_DOMAIN = b"thinktuning-mcp-catalog-cursor-v1"

# Longueur de la signature tronquée (128 bits : au-delà, coût nul et taille +).
_SIG_BYTES = 16

_CURSOR_VERSION = 1

# Catalogues paginés (kind du curseur) — évite le re-jeu croisé de curseurs.
KIND_TOOLS = "tools"
KIND_RESOURCES = "resources"
KIND_PROMPTS = "prompts"

DEFAULT_PAGE_SIZE = 50
DEFAULT_CURSOR_TTL_SECONDS = 900

# Secret par process (lazy) si aucune clé d'environnement.
_PROCESS_SECRET: bytes | None = None


class CursorError(ValueError):
    """Curseur MCP invalide (malformé, falsifié, expiré ou d'un autre kind)."""

    def __init__(self, reason: str, *, expired: bool = False) -> None:
        super().__init__(reason)
        self.expired = expired


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Entier d'environnement tolérant : valeur absente/invalide → défaut."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        return default


def default_page_size() -> int:
    """Taille de page par défaut (configurable, min 1)."""
    return _env_int("MCP_PAGINATION_PAGE_SIZE", DEFAULT_PAGE_SIZE)


def cursor_ttl_seconds() -> int:
    """TTL des curseurs en secondes (configurable, min 1)."""
    return _env_int("MCP_PAGINATION_CURSOR_TTL_SECONDS", DEFAULT_CURSOR_TTL_SECONDS)


def _resolve_secret(override: bytes | None) -> bytes:
    """Secret HMAC : override > env ``MCP_PAGINATION_SECRET`` > secret process."""
    if override is not None:
        return override
    env_secret = os.getenv("MCP_PAGINATION_SECRET", "").strip()
    if env_secret:
        return env_secret.encode("utf-8")
    global _PROCESS_SECRET
    if _PROCESS_SECRET is None:
        _PROCESS_SECRET = secrets.token_bytes(32)
    return _PROCESS_SECRET


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(encoded: str) -> bytes:
    padding = "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode(encoded + padding)
    except (binascii.Error, ValueError) as exc:
        raise CursorError("cursor is malformed (bad base64)") from exc


def _sign(secret: bytes, body: str) -> str:
    digest = hmac.new(secret, _CURSOR_DOMAIN + body.encode("ascii"), hashlib.sha256)
    return _b64url_encode(digest.digest()[:_SIG_BYTES])


def encode_cursor(
    kind: str,
    offset: int,
    *,
    issued_at: float | None = None,
    secret: bytes | None = None,
) -> str:
    """Produit un curseur opaque : ``base64url(payload).base64url(hmac)``.

    Args:
        kind: catalogue émetteur (``KIND_TOOLS`` / ``KIND_RESOURCES`` /
            ``KIND_PROMPTS``) — lié au curseur pour rejeter le re-jeu croisé.
        offset: index (0-based) du premier item de la page SUIVANTE.
        issued_at: horodatage d'émission (epoch seconds, défaut : maintenant) —
            paramètre de test ; en production, l'horloge du serveur tranche.
        secret: secret HMAC d'override (tests) — défaut : env ou process.
    """
    payload = {
        "v": _CURSOR_VERSION,
        "k": kind,
        "o": max(0, int(offset)),
        "t": round(float(time.time() if issued_at is None else issued_at), 3),
    }
    body = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{body}.{_sign(_resolve_secret(secret), body)}"


def decode_cursor(
    cursor: str,
    kind: str,
    *,
    now: float | None = None,
    ttl_seconds: int | None = None,
    secret: bytes | None = None,
) -> int:
    """Vérifie un curseur opaque et retourne l'offset de reprise.

    Args:
        cursor: valeur opaque renvoyée telle quelle par le client.
        kind: catalogue attendu (doit correspondre au kind signé).
        now: horloge d'override (tests) — défaut : ``time.time()``.
        ttl_seconds: TTL d'override (tests) — défaut : env
            ``MCP_PAGINATION_CURSOR_TTL_SECONDS`` (900 s).
        secret: secret HMAC d'override (tests).

    Returns:
        L'offset (0-based) du premier item de la page demandée.

    Raises:
        CursorError: curseur malformé, signature invalide (falsifié), version
            inconnue, kind différent ou EXPIRÉ (``expired=True``).
    """
    resolved_secret = _resolve_secret(secret)
    parts = cursor.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise CursorError("cursor is malformed (expected '<payload>.<signature>')")
    body, signature = parts
    expected = _sign(resolved_secret, body)
    if not hmac.compare_digest(signature, expected):
        raise CursorError("cursor signature mismatch (tampered or foreign cursor)")
    try:
        payload: Any = json.loads(_b64url_decode(body))
    except CursorError as exc:
        raise CursorError("cursor is malformed (bad base64)") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != _CURSOR_VERSION
        or not isinstance(payload.get("o"), int)
        or not isinstance(payload.get("k"), str)
        or int(payload["o"]) < 0
    ):
        raise CursorError("cursor payload is invalid")
    if payload["k"] != kind:
        raise CursorError(f"cursor was issued for '{payload['k']}', not '{kind}'")
    issued_at = payload.get("t")
    if not isinstance(issued_at, (int, float)):
        raise CursorError("cursor payload is invalid (missing timestamp)")
    resolved_now = time.time() if now is None else now
    resolved_ttl = cursor_ttl_seconds() if ttl_seconds is None else ttl_seconds
    if resolved_now - float(issued_at) > resolved_ttl:
        raise CursorError("cursor expired", expired=True)
    return int(payload["o"])


def paginate(
    items: list[Any], page_size: int, offset: int
) -> tuple[list[Any], int | None]:
    """Découpe ``items`` en page — retourne ``(page, next_offset | None)``.

    ``next_offset`` est ``None`` quand la page couvre la fin de la liste
    (dernière page : aucun ``nextCursor`` à émettre).
    """
    effective = max(1, int(page_size))
    start = max(0, int(offset))
    page = items[start : start + effective]
    next_offset = start + effective if start + effective < len(items) else None
    return page, next_offset


__all__ = [
    "DEFAULT_CURSOR_TTL_SECONDS",
    "DEFAULT_PAGE_SIZE",
    "CursorError",
    "KIND_PROMPTS",
    "KIND_RESOURCES",
    "KIND_TOOLS",
    "cursor_ttl_seconds",
    "decode_cursor",
    "default_page_size",
    "encode_cursor",
    "paginate",
]

