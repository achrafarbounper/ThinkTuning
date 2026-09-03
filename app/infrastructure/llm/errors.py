"""Classification des erreurs HTTP pour le client LLM v2 (httpx).

Réutilise ``ErrorClass`` / ``ErrorCategory`` du module legacy
``ia.agent.reliability`` pour garder les sérialisations (``to_dict()``)
alignées avec le v1 — même sémantique de log, même payload d'API.

``retry()`` et ``CircuitBreaker`` de ``ia/agent/reliability.py`` sont des
briques génériques (aucune dépendance à ``requests``) : seul le classifieur
est ici adapté à ``httpx``, car les exceptions diffèrent (``httpx.TimeoutException``
vs ``requests.exceptions.Timeout``, etc.).
"""

from __future__ import annotations

import httpx

from ia.agent.reliability import CircuitBreaker, ErrorCategory, ErrorClass

# Statuts HTTP considérés comme éphémères / idempotents à re-tenter (aligné
# sur la valeur du module legacy ``ia/agent/reliability.py``).
RETRYABLE_HTTP_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 507})


def classify_llm_error(exc: BaseException) -> ErrorClass:
    """Classe une exception httpx en famille + réessayable ou non.

    - ``httpx.TimeoutException``      -> timeout, réessayable ;
    - ``httpx.ConnectError``          -> connexion, réessayable ;
    - ``httpx.ProtocolError``         -> protocole, réessayable ;
    - ``httpx.NetworkError``          -> connexion (repli), réessayable ;
    - ``httpx.HTTPStatusError``       -> selon le statut (retryable si éphémère) ;
    - ``CircuitBreaker.CallNotPermitted`` -> circuit, définitif (re-tenter
      n'aurait aucun sens tant que le breaker est ouvert) ;
    - ``httpx.RequestError``          -> protocole (repli générique) ;
    - erreur quelconque               -> inconnue, DÉFINITIVE (échec fermé :
      sans politique explicite on ne re-tente jamais).
    """
    if isinstance(exc, httpx.TimeoutException):
        return ErrorClass(ErrorCategory.TIMEOUT, True, reason="httpx timeout")
    if isinstance(exc, httpx.ConnectError):
        return ErrorClass(ErrorCategory.CONNECTION, True, reason="httpx connect")
    if isinstance(exc, httpx.ProtocolError):
        return ErrorClass(ErrorCategory.PROTOCOL, True, reason="httpx protocol")
    if isinstance(exc, httpx.NetworkError):
        return ErrorClass(ErrorCategory.CONNECTION, True, reason="httpx network")
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code if exc.response is not None else None
        retryable = status in RETRYABLE_HTTP_STATUS
        return ErrorClass(
            ErrorCategory.HTTP, retryable, http_status=status,
            reason=f"http {status}",
        )
    if isinstance(exc, CircuitBreaker.CallNotPermitted):
        return ErrorClass(ErrorCategory.CIRCUIT, False, reason="circuit open")
    if isinstance(exc, httpx.RequestError):
        return ErrorClass(ErrorCategory.PROTOCOL, True, reason="httpx request")
    return ErrorClass(ErrorCategory.UNKNOWN, False, reason=type(exc).__name__)


__all__ = ["ErrorClass", "ErrorCategory", "RETRYABLE_HTTP_STATUS", "classify_llm_error"]
