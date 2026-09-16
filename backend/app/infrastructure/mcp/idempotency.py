# project/app/infrastructure/mcp/idempotency.py
"""Idempotence des appels MCP (L2 — SCRUM-153).

Problème résolu : le transport MCP est appelé par des clients qui RÉESSAIENT
sur timeout proxy, sur coupure réseau ou sur ``Retry-After`` (backpressure).
Sans clé d'idempotence, un réessai relance un run multi-agent complet (coût
LLM + effets de bord des outils) alors que la première exécution a peut-être
déjà abouti.

Contrat implémenté (inspiré de la sémantique ``Idempotency-Key`` d'IETF
draft-ietf-httpapi-idempotency-key-header) :

* ``new``      : première vue de la clé → exécuter, la réponse sera mémorisée ;
* ``replay``   : clé déjà TERMINÉE avec le MÊME empreinte → rejouer la réponse
  mémorisée sans réexécuter (aucun appel LLM) ;
* ``inflight`` : clé déjà RÉSERVÉE (exécution en cours) → le client doit
  réessayer après ``Retry-After`` (réponse ``409``-like, jamais un doublon) ;
* ``conflict`` : même clé mais empreinte DIFFÉRENTE → ``422`` (contrat violé :
  une clé d'idempotence ne peut pas désigner deux requêtes distinctes).

Choix d'architecture :

* **Store en mémoire** (``OrderedDict`` + TTL + éviction LRU bornée) : la
  surface MCP est unifiée par instance (``_server`` singleton du transport) —
  une clé n'a pas besoin de survie inter-processus pour couvrir la fenêtre de
  réessai (quelques secondes à quelques minutes). Aucun verrou distribué à
  ajouter, aucune migration de schéma (strangler pattern : Mongo reste la
  source de vérité pour les RUNS, pas pour les clés de réessai) ;
* **TTL distincts** : une réservation ``in_flight`` expire VITE
  (``in_flight_ttl``) pour qu'un serveur tué en plein vol ne bloque pas les
  réessais, tandis qu'une réponse ``completed`` survit plus longtemps (fenêtre
  de réessai du client) ;
* **Fingerprint** : empreinte SHA-256 canonique de la requête (la clé
  elle-même est EXCLUE du calcul — elle ne fait pas partie de la sémantique) ;
* **Thread-safe** : le transport exécute le chemin non-stream dans
  ``asyncio.to_thread`` → tout accès est protégé par un ``Lock``.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Vocabulaire public
# ---------------------------------------------------------------------------

OUTCOME_NEW = "new"
OUTCOME_REPLAY = "replay"
OUTCOME_INFLIGHT = "inflight"
OUTCOME_CONFLICT = "conflict"

VALID_IDEMPOTENCY_OUTCOMES: frozenset[str] = frozenset(
    {OUTCOME_NEW, OUTCOME_REPLAY, OUTCOME_INFLIGHT, OUTCOME_CONFLICT}
)

_STATE_IN_FLIGHT = "in_flight"
_STATE_COMPLETED = "completed"

#: En-tête HTTP standard (``Idempotency-Key``) — prioritaire sur le champ JSON.
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"

#: Champ JSON-RPC accepté en repli : ``params.arguments.idempotency_key``.
IDEMPOTENCY_KEY_ARGUMENT = "idempotency_key"

#: Bornes défensives : une clé fournie par le client est une entrée NON fiable
#: (mémoire du store + logs) → longueur plafonnée, jamais utilisée telle quelle
#: comme identifiant de ressource.
MAX_KEY_LENGTH = 200
MAX_FINGERPRINT_LENGTH = 64

DEFAULT_TTL_SECONDS = 900
DEFAULT_IN_FLIGHT_TTL_SECONDS = 120
DEFAULT_MAX_ENTRIES = 2048


def normalize_idempotency_key(value: Any) -> str | None:
    """Normalise une clé d'idempotence client (``None`` si absente/invalide).

    Une clé vide, non textuelle ou démesurée est traitée comme ABSENTE (le
    client repart alors en mode non idempotent) plutôt que de lever : un
    transport ne doit pas échouer sur une entrée métadonnée douteuse.
    """
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized or len(normalized) > MAX_KEY_LENGTH:
        return None
    return normalized


def extract_idempotency_key(
    payload: object,
    header_value: str | None = None,
) -> str | None:
    """Résout la clé d'idempotence (en-tête ``Idempotency-Key`` prioritaire)."""
    from_header = normalize_idempotency_key(header_value)
    if from_header is not None:
        return from_header
    if not isinstance(payload, Mapping):
        return None
    params = payload.get("params")
    arguments = params.get("arguments") if isinstance(params, Mapping) else None
    if not isinstance(arguments, Mapping):
        return None
    return normalize_idempotency_key(arguments.get(IDEMPOTENCY_KEY_ARGUMENT))


def fingerprint_payload(payload: object) -> str:
    """Empreinte SHA-256 canonique d'une requête MCP (clé d'idempotence exclue).

    La sérialisation est TRIÉE et sans espaces superflus : deux requêtes
    logiquement identiques mais sérialisées différemment produisent la même
    empreinte (sinon chaque réessai serait vu comme un conflit).
    """
    sanitized = _without_idempotency_key(payload)
    try:
        canonical = json.dumps(
            sanitized,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        canonical = repr(sanitized)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:MAX_FINGERPRINT_LENGTH]


def _without_idempotency_key(payload: object) -> object:
    """Copie du payload SANS ``params.arguments.idempotency_key`` (pure).

    L4 (SCRUM-155) : la cle d'idempotence est une METADONNEE de transport, pas
    une partie de la semantique de la requete -- elle ne doit donc jamais
    entrer dans l'empreinte. Avant ce correctif, la fonction hachait le payload
    complet alors que son contrat documente annoncait l'exclusion : un client
    envoyant la cle en en-tete au premier appel puis dans le corps au reessai
    (ou l'inverse) se voyait refuser un ``422 conflict`` pour une requete
    pourtant identique.

    Aucune mutation de l'entree : le payload du transport reste intact.
    """
    if not isinstance(payload, Mapping):
        return payload
    params = payload.get("params")
    if not isinstance(params, Mapping):
        return payload
    arguments = params.get("arguments")
    if not isinstance(arguments, Mapping) or IDEMPOTENCY_KEY_ARGUMENT not in arguments:
        return payload
    sanitized_arguments = {
        key: value for key, value in arguments.items() if key != IDEMPOTENCY_KEY_ARGUMENT
    }
    return {**payload, "params": {**params, "arguments": sanitized_arguments}}


# ---------------------------------------------------------------------------
# Enregistrements
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IdempotencyRecord:
    """Réservation/résultat mémorisé pour une clé d'idempotence."""

    key: str
    fingerprint: str
    state: str = _STATE_IN_FLIGHT
    result: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    @property
    def is_in_flight(self) -> bool:
        return self.state == _STATE_IN_FLIGHT

    @property
    def is_completed(self) -> bool:
        return self.state == _STATE_COMPLETED

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "fingerprint": self.fingerprint,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "result_size": len(self.result or ""),
        }


@dataclass(frozen=True)
class IdempotencyDecision:
    """Verdict rendu par ``IdempotencyStore.reserve``."""

    outcome: str
    record: IdempotencyRecord | None = None

    @property
    def should_execute(self) -> bool:
        return self.outcome == OUTCOME_NEW

    @property
    def is_replay(self) -> bool:
        return self.outcome == OUTCOME_REPLAY

    @property
    def is_in_flight(self) -> bool:
        return self.outcome == OUTCOME_INFLIGHT

    @property
    def is_conflict(self) -> bool:
        return self.outcome == OUTCOME_CONFLICT

    @property
    def replay_result(self) -> str | None:
        """Réponse mémorisée à rejouer (``None`` si pas un replay)."""
        if self.outcome != OUTCOME_REPLAY or self.record is None:
            return None
        return self.record.result


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class IdempotencyStore:
    """Registre borné (TTL + LRU) des clés d'idempotence MCP, thread-safe."""

    def __init__(
        self,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        in_flight_ttl_seconds: int = DEFAULT_IN_FLIGHT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = max(1, int(ttl_seconds))
        self._in_flight_ttl = max(1, int(in_flight_ttl_seconds))
        self._max_entries = max(1, int(max_entries))
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, IdempotencyRecord] = OrderedDict()

    # -- lecture ------------------------------------------------------------
    def get(self, key: str) -> IdempotencyRecord | None:
        normalized = normalize_idempotency_key(key)
        if normalized is None:
            return None
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            record = self._entries.get(normalized)
            if record is not None:
                self._entries.move_to_end(normalized)
            return record

    def size(self) -> int:
        with self._lock:
            return len(self._entries)

    def snapshot(self) -> list[dict[str, Any]]:
        """Vue sérialisable des clés en cours (diagnostic, sans secret)."""
        with self._lock:
            return [record.as_dict() for record in self._entries.values()]

    # -- écriture -----------------------------------------------------------
    def reserve(self, key: str, fingerprint: str) -> IdempotencyDecision:
        """Réserve la clé (ou révèle replay / in-flight / conflit).

        ``new`` : la clé est désormais EN VOL — l'appelant DOIT appeler
        :meth:`complete` (succès) ou :meth:`release` (échec) pour que les
        réessais suivants soient corrects.
        """
        normalized = normalize_idempotency_key(key)
        if normalized is None:
            # Pas de clé exploitable : mode non idempotent (comportement L1).
            return IdempotencyDecision(OUTCOME_NEW, None)
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            existing = self._entries.get(normalized)
            if existing is not None:
                self._entries.move_to_end(normalized)
                if existing.fingerprint != str(fingerprint):
                    return IdempotencyDecision(OUTCOME_CONFLICT, existing)
                if existing.is_in_flight:
                    return IdempotencyDecision(OUTCOME_INFLIGHT, existing)
                return IdempotencyDecision(OUTCOME_REPLAY, existing)
            record = IdempotencyRecord(
                key=normalized,
                fingerprint=str(fingerprint),
                state=_STATE_IN_FLIGHT,
                result=None,
                created_at=now,
                updated_at=now,
                expires_at=now + self._in_flight_ttl,
            )
            self._entries[normalized] = record
            self._evict_locked()
            return IdempotencyDecision(OUTCOME_NEW, record)

    def complete(self, key: str, fingerprint: str, result: str) -> None:
        """Mémorise la réponse d'une exécution réussie (rejouable ensuite)."""
        normalized = normalize_idempotency_key(key)
        if normalized is None:
            return
        now = self._clock()
        with self._lock:
            existing = self._entries.get(normalized)
            if existing is None or existing.fingerprint != str(fingerprint):
                return
            self._entries[normalized] = IdempotencyRecord(
                key=normalized,
                fingerprint=existing.fingerprint,
                state=_STATE_COMPLETED,
                result=str(result),
                created_at=existing.created_at,
                updated_at=now,
                expires_at=now + self._ttl,
            )
            self._entries.move_to_end(normalized)

    def release(self, key: str, fingerprint: str | None = None) -> bool:
        """Libère une clé dont l'exécution a ÉCHOUÉ (réessai autorisé).

        On SUPPRIME l'entrée au lieu de la marquer en échec : la sémantique
        attendue d'une clé d'idempotence est « un échec reste retentable ».
        Une réponse réussie mémorisée n'est jamais invalidée.
        """
        normalized = normalize_idempotency_key(key)
        if normalized is None:
            return False
        with self._lock:
            existing = self._entries.get(normalized)
            if existing is None:
                return False
            if fingerprint is not None and existing.fingerprint != str(fingerprint):
                return False
            if existing.is_completed:
                return False
            del self._entries[normalized]
            return True

    def purge_expired(self) -> int:
        with self._lock:
            return self._purge_locked(self._clock())

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # -- interne ------------------------------------------------------------
    def _purge_locked(self, now: float) -> int:
        expired = [key for key, record in self._entries.items() if record.is_expired(now)]
        for key in expired:
            del self._entries[key]
        return len(expired)

    def _evict_locked(self) -> None:
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


# ---------------------------------------------------------------------------
# Singleton de transport (injectable pour les tests)
# ---------------------------------------------------------------------------

_store: IdempotencyStore | None = None
_store_lock = threading.Lock()


def get_idempotency_store() -> IdempotencyStore:
    """Store partagé du processus (paresseux : aucune I/O, aucun import lourd)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = IdempotencyStore()
        return _store


def configure_idempotency_store(store: IdempotencyStore | None) -> None:
    """Injecte (ou réinitialise avec ``None``) le store utilisé par le transport."""
    global _store
    with _store_lock:
        _store = store


__all__ = [
    "DEFAULT_IN_FLIGHT_TTL_SECONDS",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_SECONDS",
    "IDEMPOTENCY_KEY_ARGUMENT",
    "IDEMPOTENCY_KEY_HEADER",
    "IdempotencyDecision",
    "IdempotencyRecord",
    "IdempotencyStore",
    "MAX_FINGERPRINT_LENGTH",
    "MAX_KEY_LENGTH",
    "OUTCOME_CONFLICT",
    "OUTCOME_INFLIGHT",
    "OUTCOME_NEW",
    "OUTCOME_REPLAY",
    "VALID_IDEMPOTENCY_OUTCOMES",
    "configure_idempotency_store",
    "extract_idempotency_key",
    "fingerprint_payload",
    "get_idempotency_store",
    "normalize_idempotency_key",
]
