"""Fiabilité des appels LLM : retry à backoff exponentiel, circuit breaker et
classification des erreurs. Phase A — activé par le flag ``AGENT_RELIABILITY``.

Trois briques indépendantes et composables :

    - ``classify_llm_error(exc)`` -> ``ErrorClass`` : classe une exception en
      RÉESSAYABLE (timeout, indisponibilité réseau, 5xx/service éphémère) ou
      DÉFINITIVE (authentification, requête invalide). Une erreur réessayable
      est soumise au retry + au circuit breaker ; une erreur définitive est
      remontée immédiatement sans consommer la santé du circuit.
    - ``retry(op, ...)``          -> exécute *op* jusqu'à *attempts* fois, avec
      un backoff exponentiel + jitter entre les tentatives, en n'utilisant que
      les échecs marqués réessayables par *classify*. Retourne le résultat de la
      première exécution réussie, sinon relève la dernière exception.
    - ``CircuitBreaker``          -> interrompt les appels vers un endpoint en
      échec (au-delà de ``failures_max``) pendant ``cooldown`` secondes, puis
      laisse passer une sonde en demi-ouvert ; revient à l'état fermé après
      succès. État inspectable, remise à zéro explicite via ``reset()``.

Le tout est thread-safe (le breaker est protégé par un verrou ; l'accumulateur
de retry est local à chaque appel). Même canal de log que le reste de l'agent
(« thinktuning.agent »).
"""

import logging
import os
import random
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger("thinktuning.agent")
logger.setLevel(os.getenv("AGENT_LOG_LEVEL", "INFO").upper())


# ============================================================
# CLASSIFICATION DES ERREURS
# ============================================================


class ErrorCategory(str, Enum):
    """Familles d'erreurs remontées par le client LLM / le réseau."""

    TIMEOUT = "timeout"        # délai dépassé (requests.Timeout)
    CONNECTION = "connection"  # endpoint injoignable (requests.ConnectionError)
    HTTP = "http"              # réponse HTTP avec statut hors 2xx
    PROTOCOL = "protocol"      # erreur de flux / parsing (RequestException)
    CIRCUIT = "circuit"        # appel refusé par le circuit breaker
    UNKNOWN = "unknown"        # erreur non classée


# Statuts HTTP considérés comme éphémères / idempotents à re-tenter.
_RETRYABLE_HTTP_STATUS = {408, 425, 429, 500, 502, 503, 504, 507}


@dataclass(frozen=True)
class ErrorClass:
    """Classification d'une exception : famille, réessayable ou non, statut.

    ``http_status`` n'est renseigné que pour la famille ``http``. ``reason``
    est une chaîne courte lisible pour les logs / la trace.
    """

    category: ErrorCategory
    retryable: bool
    http_status: Optional[int] = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "category": self.category.value,
            "retryable": self.retryable,
            "http_status": self.http_status,
            "reason": self.reason,
        }


def classify_llm_error(exc: BaseException) -> ErrorClass:
    """Classe *exc* en fonction de sa nature (requests / circuit / générique).

    - ``requests.exceptions.Timeout``        -> timeout, réessayable ;
    - ``requests.exceptions.ConnectionError`` -> connexion, réessayable ;
    - ``requests.exceptions.HTTPError``       -> selon le statut (429, 5xx
      éphémères = réessayable ; 4xx métier = définitif) ;
    - ``requests.exceptions.RequestException``-> protocole, réessayable ;
    - ``CircuitBreaker.CallNotPermitted``    -> circuit, définitif (remonté tel
      quel : re-tenter n'aurait aucun sens tant que le breaker est ouvert) ;
    - erreur quelconque                      -> inconnue, DÉFINITIVE (échec
      fermé : sans politique explicite on ne re-tente jamais).
    """
    # requests importé paresseusement : module toujours présent en runtime,
    # mais on tolère son absence (tests / utilitaires isolés).
    try:
        import requests

        _have_requests = True
    except Exception:  # pragma: no cover - environnement sans requests
        _have_requests = False

    if _have_requests:
        if isinstance(exc, requests.exceptions.Timeout):
            return ErrorClass(ErrorCategory.TIMEOUT, True, reason="timeout")
        if isinstance(exc, requests.exceptions.ConnectionError):
            return ErrorClass(ErrorCategory.CONNECTION, True, reason="connection")
        if isinstance(exc, requests.exceptions.HTTPError):
            status = exc.response.status_code if exc.response is not None else None
            if status is not None and status in _RETRYABLE_HTTP_STATUS:
                return ErrorClass(
                    ErrorCategory.HTTP, True, status, reason="server_or_rate"
                )
            return ErrorClass(ErrorCategory.HTTP, False, status, reason="client")
        if isinstance(exc, requests.exceptions.RequestException):
            # Erreur pendant la lecture du flux / encodage : réessayable une
            # fois en pratique (la connexion a pu être coupée avant la réponse).
            return ErrorClass(ErrorCategory.PROTOCOL, True, reason="request")

    if exc.__class__.__name__ == "CallNotPermitted":
        return ErrorClass(ErrorCategory.CIRCUIT, False, reason="circuit_open")

    return ErrorClass(ErrorCategory.UNKNOWN, False, reason=type(exc).__name__)
# ============================================================
# RETRY À BACKOFF EXPONENTIEL
# ============================================================


def retry(
    op: Callable[[], Any],
    attempts: int = 3,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    jitter: float = 0.2,
    classify: Optional[Callable[[BaseException], ErrorClass]] = None,
    on_retry: Optional[Callable[[int, BaseException, Optional[ErrorClass]], None]] = None,
) -> Any:
    """Exécute ``op()`` jusqu'à *attempts* fois avec backoff exponentiel.

    - Chaque tentative est capturée ; on ne re-tente que si *classify* marque
      l'erreur comme réessayable (``retryable is True``) ET qu'il reste des
      tentatives.
    - Entre deux tentatives on attend ``base_delay * 2**(tentative-1)``
      secondes (borné par *max_delay*), plus un jitter aléatoire pour éviter
      l'effet de « thundering herd ».
    - ``on_retry(attempt, exc, error_class)`` est appelé pour CHAQUE échec
      (avant la décision de re-tenter) : sert au logging / à la télémétrie.
    - Si *classify* est None, toute exception est considérée définitive
      (comportement strict : pas de retry sans politique explicite).

    Retour : résultat de la première exécution réussie ; sinon relève la
    dernière exception.
    """
    attempts = max(1, int(attempts))
    last_exc: Optional[BaseException] = None

    for attempt in range(1, attempts + 1):
        try:
            return op()
        except BaseException as exc:  # noqa: BLE001 - re-levée après le retry
            last_exc = exc
            ec = classify(exc) if classify is not None else None
            retryable = ec is not None and ec.retryable and attempt < attempts
            if on_retry is not None:
                on_retry(attempt, exc, ec)
            if not retryable:
                logger.debug(
                    "retry_stop attempt=%d/%d retryable=%s category=%s err=%s",
                    attempt, attempts, retryable,
                    ec.category.value if ec else "?",
                    type(exc).__name__,
                )
                raise exc
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            if jitter > 0:
                delay = max(0.0, delay * (1.0 + random.uniform(-jitter, jitter)))
            logger.warning(
                "retry_wait attempt=%d/%d sleep=%.2fs category=%s err=%s",
                attempt, attempts, delay,
                ec.category.value if ec else "?",
                type(exc).__name__,
            )
            time.sleep(delay)

    # Atteint uniquement si attempts == 0 (empêché par le `max(1, ...)`).
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("retry(): aucune tentative effectuée")


# ============================================================
# CIRCUIT BREAKER
# ============================================================


class CircuitBreaker:
    """Interrupteur de circuit : coupe les appels vers un endpoint en échec.

    Machine à états (thread-safe) :

        - ``closed``    : les appels passent ; chaque échec incrémente un
          compteur. Dès que ``failures >= failures_max`` -> ``open``.
        - ``open``      : les appels sont refusés immédiatement
          (``CallNotPermitted``) pendant ``cooldown_seconds``, puis -> ``half_open``.
        - ``half_open`` : une seule SONDE est autorisée. Succès -> ``closed``
          (compteur remis à zéro) ; échec -> ``open`` de nouveau.

    Le compteur est en mémoire (pas de persistance inter-processus) : suffisant
    pour un endpoint LLM unique derrière l'agent.
    """

    class CallNotPermitted(Exception):
        """Levée quand le circuit est ouvert : l'appel n'est pas exécuté."""

    def __init__(
        self,
        name: str = "default",
        failures_max: int = 5,
        cooldown_seconds: float = 30.0,
        reset_on_success: bool = True,
    ):
        self.name = name
        self.failures_max = max(1, int(failures_max))
        self.cooldown_seconds = float(cooldown_seconds)
        self.reset_on_success = bool(reset_on_success)
        self._lock = threading.RLock()
        self._state = "closed"
        self._failures = 0
        self._opened_at = 0.0
        self._probe_held = False
# --- Logique de décision ----------------------------------------------------

    def _maybe_enter_half_open(self) -> None:
        """Passe ``open`` -> ``half_open`` quand le cooldown est écoulé."""
        if self._state == "open" and time.monotonic() - self._opened_at >= self.cooldown_seconds:
            logger.info("circuit_half_open name=%s (sonde autorisée)", self.name)
            self._state = "half_open"
            self._probe_held = False

    def _open(self) -> None:
        self._state = "open"
        self._opened_at = time.monotonic()
        self._failures = 0
        self._probe_held = False
        logger.error(
            "circuit_open name=%s cooldown=%.1fs", self.name, self.cooldown_seconds
        )

    def _record_failure(self) -> None:
        with self._lock:
            if self._state == "half_open":
                # La SONDE (unique autorisée) a échoué : on re-ouvre le circuit
                # immédiatement, quel que soit le compteur cumulé.
                self._open()
                return
            if self._state == "closed":
                self._failures += 1
                if self._failures >= self.failures_max:
                    self._open()

    def _record_success(self) -> None:
        with self._lock:
            if self.reset_on_success:
                self._failures = 0
            if self._state == "half_open":
                logger.info("circuit_closed name=%s (sonde réussie)", self.name)
                self._state = "closed"
                self._failures = 0

    def call(self, op: Callable[[], Any]) -> Any:
        """Exécute ``op()`` à travers le circuit.

        - Si le circuit est ``open``, lève ``CallNotPermitted`` SANS appeler ``op``.
        - En ``half_open``, n'autorise qu'UNE SEULE sonde à la fois.
        - Sinon exécute ``op``, enregistre succès/échec et applique les
          transitions : échec -> compteur (éventuellement open) ; succès ->
          reset éventuel + passage half_open -> closed.
        """
        with self._lock:
            self._maybe_enter_half_open()
            if self._state == "open":
                logger.warning(
                    "circuit_call_blocked name=%s state=open cooldown_remaining=%.1fs",
                    self.name,
                    max(0.0, self.cooldown_seconds - (time.monotonic() - self._opened_at)),
                )
                raise CircuitBreaker.CallNotPermitted(
                    f"circuit '{self.name}' ouvert : appels refusés jusqu'à "
                    "expiration du cooldown"
                )
            half_open_probe = self._state == "half_open"
            if half_open_probe and self._probe_held:
                logger.warning(
                    "circuit_call_blocked name=%s state=half_open probe_in_flight",
                    self.name,
                )
                raise CircuitBreaker.CallNotPermitted(
                    f"circuit '{self.name}' en demi-ouvert : une sonde est déjà en cours"
                )
            if half_open_probe:
                self._probe_held = True

        try:
            result = op()
        except BaseException as exc:  # noqa: BLE001 - propagée après comptage
            self._record_failure()
            raise
        else:
            self._record_success()
            return result
        finally:
            if half_open_probe:
                with self._lock:
                    self._probe_held = False

    def reset(self) -> None:
        """Remise à zéro : état fermé, compteur et sonde réinitialisés."""
        with self._lock:
            self._state = "closed"
            self._failures = 0
            self._opened_at = 0.0
            self._probe_held = False
            logger.info("circuit_reset name=%s", self.name)

    # --- Introspection ---------------------------------------------------------

    @property
    def state(self) -> str:
        """État courant (closed / open / half_open), fractionnement appliqué."""
        with self._lock:
            self._maybe_enter_half_open()
            return self._state

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    def status(self) -> dict:
        """État lisible pour la télémétrie / l'UI."""
        with self._lock:
            self._maybe_enter_half_open()
            return {
                "name": self.name,
                "state": self._state,
                "failures": self._failures,
                "failures_max": self.failures_max,
            }