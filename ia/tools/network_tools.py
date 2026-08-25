"""Outils réseau de l'agent : HTTP GET / POST (via `requests`, déjà présent).

Sécurité :
    - schémas http/https uniquement ;
    - sortie tronquée (max_chars) pour ne pas saturer le LLM ;
    - politique anti-SSRF optionnelle : AGENT_BLOCK_PRIVATE_HOSTS=1 interdit
      les hôtes privés/loopback (utile si l'API est exposée au-delà du poste).
"""

import requests

from .sandbox import enforce_host_policy, truncate_output, url_scheme_allowed

DEFAULT_TIMEOUT_S = 30.0
DEFAULT_MAX_CHARS = 8000
_TIMEOUT_MIN, _TIMEOUT_MAX = 1.0, 120.0


def _clean_timeout(timeout: float) -> float:
    return max(_TIMEOUT_MIN, min(float(timeout), _TIMEOUT_MAX))


def _response_to_dict(resp: requests.Response, max_chars: int) -> dict:
    return {
        "status": resp.status_code,
        "reason": resp.reason,
        "url": resp.url,
        "content_type": resp.headers.get("content-type"),
        "body": truncate_output(resp.text, max_chars),
    }


# --- GET ------------------------------------------------------------------------
def http_get(url: str, headers: dict | None = None, timeout: float = DEFAULT_TIMEOUT_S,
             max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """GET HTTP : renvoie {status, reason, url, content_type, body tronqué}.

    Ne lève PAS sur 4xx/5xx : le code HTTP est retourné tel quel pour que
    l'agent puisse raisonner dessus.
    """
    url_scheme_allowed(url)
    enforce_host_policy(url)
    resp = requests.get(
        url, headers=dict(headers or {}), timeout=_clean_timeout(timeout)
    )
    return _response_to_dict(resp, max(50, int(max_chars)))


# --- POST -------------------------------------------------------------------------
def http_post(url: str, data: str | None = None, json_payload: dict | None = None,
              headers: dict | None = None, timeout: float = DEFAULT_TIMEOUT_S,
              max_chars: int = DEFAULT_MAX_CHARS) -> dict:
    """POST HTTP : corps brut (`data`) ou JSON (`json_payload`), mutuellement exclusifs."""
    url_scheme_allowed(url)
    enforce_host_policy(url)
    if data is not None and json_payload is not None:
        raise ValueError("Utilisez 'data' OU 'json_payload', pas les deux.")

    if json_payload is not None:
        if not isinstance(json_payload, dict):
            raise ValueError("'json_payload' doit être un objet JSON ({...}).")
        kwargs = {"json": json_payload}
    else:
        kwargs = {"data": str(data) if data is not None else None}

    resp = requests.post(
        url, headers=dict(headers or {}), timeout=_clean_timeout(timeout), **kwargs
    )
    return _response_to_dict(resp, max(50, int(max_chars)))