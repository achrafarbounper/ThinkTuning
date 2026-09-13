# project/api/errors.py
"""Mapping global exceptions de domaine -> réponses HTTP.

Un SEUL handler pour toute la hiérarchie ``DomainError`` : chaque erreur
porte son ``http_status`` et son payload JSON stable (``to_payload``), donc
les routes v1 ne dupliquent AUCUN try/except HTTP. Les réponses suivent le
format projet : ``{"error": {"code", "message", "details"}}`` — le dashboard
s'accroche au ``code`` (stable), pas aux messages.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.domain.errors import DomainError

logger = logging.getLogger(__name__)


def register_domain_error_handlers(app: FastAPI) -> None:
    """Enregistre le handler global des erreurs de domaine sur l'application."""

    @app.exception_handler(DomainError)
    async def _handle_domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        # Log structuré côté serveur (niveau conforme à la gravité) : 5xx =
        # error, 4xx = warning (erreur client attendue, pas un incident).
        if exc.http_status >= 500:
            logger.error("DomainError [%s] : %s", exc.code, exc.message)
        else:
            logger.warning("DomainError [%s] : %s", exc.code, exc.message)
        return JSONResponse(status_code=exc.http_status, content=exc.to_payload())
