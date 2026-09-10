"""Outils réseau de l'agent : HTTP GET / POST (via `requests`, déjà présent).

Sécurité (P0 SEC F8) :
    - schémas http/https uniquement ;
    - protection SSRF ACTIVE PAR DÉFAUT (fail-closed) : hôtes privés/loopback
      interdits sauf AGENT_PRIVATE_HOST_ALLOWLIST (ex. searxng) — opt-out
      explicite AGENT_BLOCK_PRIVATE_HOSTS=0/false/no/off ;
    - redirects NON suivis automatiquement : l'hôte final est re-validé
      (anti-bypass 302 → 169.254.169.254) avant de suivre manuellement ;
    - téléchargement borné (stream + MAX_DOWNLOAD_BYTES anti-OOM) ;
    - sortie tronquée (max_chars) pour ne pas saturer le LLM.
"""

from typing import Any

import requests

from .sandbox import (
    MAX_DOWNLOAD_BYTES,
    enforce_host_policy,
    enforce_response_host_policy,
    truncate_output,
    url_scheme_allowed,
)

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_CHARS = 8000
_TIMEOUT_MIN, _TIMEOUT_MAX = 1.0, 120.0
# Redirects suivis manuellement après re-validation SSRF (borne anti-boucle).
_MAX_REDIRECTS = 5


def _clean_timeout(timeout: float) -> float:
    return max(_TIMEOUT_MIN, min(float(timeout), _TIMEOUT_MAX))


def _bounded_text(resp: requests.Response) -> str:
    """Corps de réponse borné anti-OOM (stream par chunks + plafond).

    Compatible avec les fakes de tests (objet sans iter_content : repli sur
    ``resp.text`` tronqué au plafond).
    """
    iter_content = getattr(resp, "iter_content", None)
    # Fakes de tests (requests.get monkeypatché) : pas de stream réel.
    if not callable(iter_content) or getattr(resp, "raw", None) is None:
        text = getattr(resp, "text", "") or ""
        return text[:MAX_DOWNLOAD_BYTES]
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in iter_content(chunk_size=65536, decode_unicode=False):
            if not chunk:
                continue
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8", errors="replace")
            total += len(chunk)
            if total > MAX_DOWNLOAD_BYTES:
                keep = len(chunk) - (total - MAX_DOWNLOAD_BYTES)
                if keep > 0:
                    chunks.append(chunk[:keep])
                break
            chunks.append(chunk)
    except Exception:
        pass
    raw = b"".join(chunks)
    try:
        return raw.decode(getattr(resp, "encoding", None) or "utf-8", errors="replace")
    except Exception:
        return raw.decode("utf-8", errors="replace")


def _request_no_redirect(
    method: str, url: str, *, timeout: float, **kwargs: Any
) -> requests.Response:
    """Requête SANS suivi auto de redirect + validation SSRF de chaque saut.

    Lève PermissionError si l'hôte initial ou un hôte de redirect est privé
    (hors allowlist). Suit au plus _MAX_REDIRECTS sauts 301/302/303/307/308.
    """
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        url_scheme_allowed(current)
        enforce_host_policy(current)
        resp = requests.request(method, current, timeout=timeout, allow_redirects=False, **kwargs)
        location = (resp.headers or {}).get("location")
        if resp.status_code not in (301, 302, 303, 307, 308) or not location:
            enforce_response_host_policy(resp.url or current)
            return resp
        # Redirect : résout l'URL relative AVANT validation (sinon bypass).
        from urllib.parse import urljoin

        current = urljoin(resp.url or current, location)
        enforce_host_policy(current)
    raise RuntimeError(f"Trop de redirects (>{_MAX_REDIRECTS}) pour : {url}")


def _response_to_dict(resp: requests.Response, max_chars: int) -> dict:
    return {
        "status": resp.status_code,
        "reason": resp.reason,
        "url": resp.url,
        "content_type": resp.headers.get("content-type"),
        "body": truncate_output(_bounded_text(resp), max_chars),
    }


# --- GET ------------------------------------------------------------------------
def http_get(
    url: str,
    headers: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict:
    """GET HTTP : renvoie {status, reason, url, content_type, body tronqué}.

    Ne lève PAS sur 4xx/5xx : le code HTTP est retourné tel quel pour que
    l'agent puisse raisonner dessus.
    """
    resp = _request_no_redirect(
        "GET",
        url,
        timeout=_clean_timeout(timeout),
        headers=dict(headers or {}),
    )
    return _response_to_dict(resp, max(50, int(max_chars)))


# --- POST -------------------------------------------------------------------------
def http_post(
    url: str,
    data: str | None = None,
    json_payload: dict | None = None,
    headers: dict | None = None,
    timeout: float = DEFAULT_TIMEOUT_S,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> dict:
    """POST HTTP : corps brut (`data`) ou JSON (`json_payload`), mutuellement exclusifs."""
    if data is not None and json_payload is not None:
        raise ValueError("Utilisez 'data' OU 'json_payload', pas les deux.")

    if json_payload is not None:
        if not isinstance(json_payload, dict):
            raise ValueError("'json_payload' doit être un objet JSON ({...}).")
        kwargs: dict[str, Any] = {"json": json_payload}
    else:
        kwargs = {"data": str(data) if data is not None else None}

    resp = _request_no_redirect(
        "POST", url, timeout=_clean_timeout(timeout), headers=dict(headers or {}), **kwargs
    )
    return _response_to_dict(resp, max(50, int(max_chars)))
