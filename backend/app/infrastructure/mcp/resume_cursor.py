# project/app/infrastructure/mcp/resume_cursor.py
"""Curseur de reprise OPAQUE des événements d'un run MCP durable (MCP 2.3.0).

SCRUM-163 — « Reprise des événements SSE » : un client reconnecté reprend le
flux d'un run durable soit par IDENTIFIANT (``run_id`` + ``after_sequence``,
L1 — SCRUM-152, comportement inchangé), soit par CURSEUR de reprise
(``resume_token``) émis par le serveur dans ``replay_started`` /
``replay_completed``.

Contrats (alignés sur ``catalog_pagination.py`` — curseurs de catalogues
v2.3.0, même mécanique signée) :

    - curseur OPAQUE : le client ne décode PAS le curseur, il le renvoie tel
      quel. Ici : payload JSON compact (version, run_id, séquence, émission)
      encodé base64url + signature HMAC-SHA256 tronquée — infalsifiable et
      illisible hors serveur ;
    - curseur lié au RUN (``run_id``) : un curseur obtenu pour un run est
      rejeté s'il est rejoué sur un autre (anti-re-jeu croisé) ;
    - EXPIRATION : TTL borné (``MCP_RESUME_TOKEN_TTL_SECONDS``, défaut 900 s) —
      un curseur trop ancien est rejeté avec
      ``ResumeTokenError(expired=True)``. COMPORTEMENT DOCUMENTÉ en cas de
      curseur expiré : le client reprend avec ``run_id`` + son dernier
      ``after_sequence`` connu (la séquence persistée ne périt JAMAIS — seul
      le token signé, lui, périt). Sans ``run_id`` en main, le client repart
      d'un replay complet (``after_sequence=0``) : l'anti-doublon du replay
      (watermark monotone + dé-duplication par ``sequence``) garantit une
      reprise sans événement rejoué deux fois côté client ;
    - COMPATIBILITÉ : le chemin ``run_id`` + ``after_sequence`` (L1) est
      inchangé ; le token est un ADDITIF, jamais une rupture.

Configurable :
    - ``MCP_RESUME_TOKEN_TTL_SECONDS``      : TTL des curseurs (défaut 900) ;
    - ``MCP_RESUME_TOKEN_SECRET``           : secret HMAC (défaut : secret
      aléatoire par process — les curseurs ne survivent pas à un redémarrage,
      accepté et documenté : le client retombe sur ``run_id`` + séquence) ;
    - ``MCP_RESUME_FOLLOW_POLL_SECONDS``    : intervalle de drain du mode
      ``follow`` du replay SSE (défaut 1.0 s) ;
    - ``MCP_RESUME_FOLLOW_TIMEOUT_SECONDS`` : durée max du suivi d'un run
      encore en cours (défaut 300 s) — au-delà, ``replay_completed`` est émis
      avec ``follow_timed_out: true`` (le client reprendra plus tard).

Ce module est PUR (aucune I/O, aucun état global mutable) : directement
testable, importable sans cycle ni dépendance lourde.
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

# Domaine de signature : SÉPARÉ des curseurs de catalogues (un curseur de
# pagination ne peut pas être rejoué comme curseur de reprise de run, et
# inversement — défense en profondeur).
_RESUME_DOMAIN = b"thinktuning-mcp-resume-cursor-v1"

# Longueur de la signature tronquée (128 bits : au-delà, coût nul et taille +).
_SIG_BYTES = 16

# Version du format de token : invalide tout token d'une ancienne disposition
# du payload (le HMAC couvre déjà ceci — défense en profondeur).
_TOKEN_VERSION = 1

DEFAULT_TOKEN_TTL_SECONDS = 900
DEFAULT_FOLLOW_POLL_SECONDS = 1.0
DEFAULT_FOLLOW_TIMEOUT_SECONDS = 300

# Secret par process (lazy) si aucune clé d'environnement.
_PROCESS_SECRET: bytes | None = None


class ResumeTokenError(ValueError):
    """Curseur de reprise MCP invalide (malformé, falsifié, expiré, run croisé)."""

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


def token_ttl_seconds() -> int:
    """TTL des curseurs de reprise en secondes (configurable, min 1)."""
    return _env_int("MCP_RESUME_TOKEN_TTL_SECONDS", DEFAULT_TOKEN_TTL_SECONDS)


def resume_follow_poll_seconds() -> float:
    """Intervalle de drain du mode ``follow`` (configurable, min 0.01)."""
    raw = os.getenv("MCP_RESUME_FOLLOW_POLL_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_FOLLOW_POLL_SECONDS
    try:
        return max(0.01, float(raw))
    except ValueError:
        return DEFAULT_FOLLOW_POLL_SECONDS


def resume_follow_timeout_seconds() -> float:
    """Durée max du suivi d'un run en cours (configurable, min 0.1)."""
    raw = os.getenv("MCP_RESUME_FOLLOW_TIMEOUT_SECONDS")
    if raw is None or not raw.strip():
        return DEFAULT_FOLLOW_TIMEOUT_SECONDS
    try:
        return max(0.1, float(raw))
    except ValueError:
        return DEFAULT_FOLLOW_TIMEOUT_SECONDS


def _resolve_secret(override: bytes | None) -> bytes:
    """Secret HMAC : override (tests) > env > secret aléatoire par process."""
    global _PROCESS_SECRET
    if override is not None:
        return override
    raw = os.getenv("MCP_RESUME_TOKEN_SECRET")
    if raw:
        return raw.encode("utf-8")
    if _PROCESS_SECRET is None:
        _PROCESS_SECRET = secrets.token_bytes(32)
    return _PROCESS_SECRET


def _b64url_encode(body: bytes) -> str:
    return base64.urlsafe_b64encode(body).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (binascii.Error, ValueError) as exc:
        raise ResumeTokenError("resume token is malformed (bad base64)") from exc


def _sign(secret: bytes, body: str) -> str:
    digest = hmac.new(secret, _RESUME_DOMAIN + body.encode("ascii"), hashlib.sha256)
    return digest.digest()[:_SIG_BYTES].hex()


def encode_resume_token(
    run_id: str,
    after_sequence: int,
    *,
    issued_at: float | None = None,
    secret: bytes | None = None,
) -> str:
    """Encapsule ``(run_id, after_sequence)`` en curseur opaque signé.

    Args:
        run_id: identifiant DURABLE du run (non vide).
        after_sequence: dernière séquence d'événement vue par le client (>= 0).
        issued_at: horloge d'override (tests) — défaut : ``time.time()``.
        secret: secret HMAC d'override (tests).

    Returns:
        Le curseur ``"<payload-base64url>.<signature-hex>"``.
    """
    normalized_id = str(run_id or "").strip()
    if not normalized_id:
        raise ValueError("run_id must not be empty")
    sequence = max(0, int(after_sequence))
    payload = {
        "v": _TOKEN_VERSION,
        "r": normalized_id,
        "s": sequence,
        "t": round(float(time.time() if issued_at is None else issued_at), 3),
    }
    body = _b64url_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    )
    return f"{body}.{_sign(_resolve_secret(secret), body)}"


def decode_resume_token(
    token: str,
    *,
    expected_run_id: str | None = None,
    now: float | None = None,
    ttl_seconds: int | None = None,
    secret: bytes | None = None,
) -> tuple[str, int]:
    """Vérifie un curseur de reprise et retourne ``(run_id, after_sequence)``.

    Args:
        token: valeur opaque renvoyée telle quelle par le client.
        expected_run_id: si fourni, le token doit porter EXACTEMENT ce
            ``run_id`` (anti-re-jeu croisé entre runs).
        now: horloge d'override (tests) — défaut : ``time.time()``.
        ttl_seconds: TTL d'override (tests) — défaut : env
            ``MCP_RESUME_TOKEN_TTL_SECONDS`` (900 s).
        secret: secret HMAC d'override (tests).

    Returns:
        Le couple ``(run_id, after_sequence)`` signé dans le token.

    Raises:
        ResumeTokenError: curseur malformé, signature invalide (falsifié ou
            d'un autre domaine), version inconnue, ``run_id`` différent de
            celui attendu, ou EXPIRÉ (``expired=True`` — comportement
            documenté : reprendre avec ``run_id`` + ``after_sequence``).
    """
    parts = str(token or "").split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ResumeTokenError("resume token is malformed (expected '<payload>.<signature>')")
    body, signature = parts
    expected = _sign(_resolve_secret(secret), body)
    if not hmac.compare_digest(signature, expected):
        raise ResumeTokenError("resume token signature mismatch (tampered or foreign token)")
    try:
        payload: object = json.loads(_b64url_decode(body))
    except ResumeTokenError as exc:
        raise ResumeTokenError("resume token is malformed (bad base64)") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("v") != _TOKEN_VERSION
        or not isinstance(payload.get("s"), int)
        or not isinstance(payload.get("r"), str)
        or int(payload["s"]) < 0
        or not payload["r"].strip()
    ):
        raise ResumeTokenError("resume token payload is invalid")
    if expected_run_id is not None and payload["r"] != str(expected_run_id).strip():
        raise ResumeTokenError(
            f"resume token was issued for run {payload['r']!r}, "
            f"not {str(expected_run_id).strip()!r}"
        )
    issued_at = payload.get("t")
    if not isinstance(issued_at, (int, float)):
        raise ResumeTokenError("resume token payload is invalid (missing timestamp)")
    resolved_now = time.time() if now is None else now
    resolved_ttl = token_ttl_seconds() if ttl_seconds is None else ttl_seconds
    if resolved_now - float(issued_at) > resolved_ttl:
        raise ResumeTokenError("resume token expired", expired=True)
    return str(payload["r"]), int(payload["s"])


__all__ = [
    "DEFAULT_FOLLOW_POLL_SECONDS",
    "DEFAULT_FOLLOW_TIMEOUT_SECONDS",
    "DEFAULT_TOKEN_TTL_SECONDS",
    "ResumeTokenError",
    "decode_resume_token",
    "encode_resume_token",
    "resume_follow_poll_seconds",
    "resume_follow_timeout_seconds",
    "token_ttl_seconds",
]
