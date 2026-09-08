# project/app/infrastructure/legacy_errors.py
"""Conversion générique des erreurs HTTP legacy en erreurs de domaine.

Les adaptateurs strangler enveloppent des handlers FastAPI legacy qui
signalent leurs échecs métier par ``HTTPException``. Ce module centralise
la traduction statut -> classe de domaine pour que chaque adaptateur
n'exprime que ses SPÉCIFICITÉS (overrides) et non la table commune :

    400 -> BadRequestError      (requête sémantiquement incorrecte)
    404 -> NotFoundError
    409 -> ConflictError
    422 -> ValidationError

Les statuts sans équivalent dans la table (502, 504...) sont re-levés
tels quels SAUF si l'adaptateur fournit un override — ex. le domaine
agent mappe 502/503/504 (``AgentRunError`` / ``ServiceUnavailableError``
/ ``GatewayTimeoutError``) et le domaine ML mappe 503
(``ModelNotAvailableError``).
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import HTTPException

from app.domain.errors import (
    BadRequestError,
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
)

DEFAULT_STATUS_TO_ERROR: dict[int, type[DomainError]] = {
    400: BadRequestError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
}


def convert_legacy_http_error(
    exc: HTTPException,
    *,
    status_overrides: Mapping[int, type[DomainError]] | None = None,
) -> DomainError:
    """Traduit une ``HTTPException`` legacy en erreur de domaine équivalente.

    ``status_overrides`` complète la table commune (503/502/504 selon le
    domaine). Les statuts toujours inconnus sont re-levés : FastAPI gardera
    sa réponse d'origine (parité totale plutôt qu'enveloppe approximative).
    """
    mapping = {**DEFAULT_STATUS_TO_ERROR, **(status_overrides or {})}
    error_cls = mapping.get(exc.status_code)
    if error_cls is None:
        raise exc
    return error_cls(str(exc.detail))
