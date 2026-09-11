# project/api/middlewares/request_id.py
"""Tracing par ``X-Request-Id`` (P2 durable, lot 16).

Chaque requête HTTP reçoit un identifiant unique :
    - généré si absent (header entrant ou uuid4), exposé en réponse
      (``X-Request-Id``) et accessible via ``request.state.request_id`` ;
    - propagé aux logs structurés (les middlewares intérieurs l'ajoutent
      à leurs lignes — cf. ``metrics.py``) et à l'audit
      (``AuditStore.log(request_id=...)`` — la colonne existe déjà).

Le header entrant est accepté UNIQUEMENT s'il est raisonnable (<= 128 chars,
charset [A-Za-z0-9._-]) : réinjecter un blob dans les logs est un vecteur
d'injection de logs. Sinon, il est RÉGÉNÉRÉ.
"""

import logging
import re
import uuid

from fastapi import Request

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "x-request-id"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _sanitize_incoming(value: str | None) -> str | None:
    if value and REQUEST_ID_PATTERN.match(value):
        return value
    return None


async def request_id_middleware(request: Request, call_next):
    incoming = _sanitize_incoming(request.headers.get(REQUEST_ID_HEADER))
    request_id = incoming or uuid.uuid4().hex
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers[REQUEST_ID_HEADER] = request_id
    return response


def get_request_id(request: Request) -> str:
    """Identifiant de la requête courante (repli générique pour l'audit)."""
    return getattr(request.state, "request_id", "unknown")
