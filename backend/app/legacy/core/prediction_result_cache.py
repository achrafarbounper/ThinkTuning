"""Cache de RÉSULTATS de prédiction (LRU + TTL).

Différent de ``core/predictor_cache.py`` (qui met en cache les INSTANCES de
modèles) : ici on met en cache les résultats déjà calculés pour un texte,
afin d'éviter de re-tokeniser / re-inférer les entrées répétées.

Thread-safe : les accès sont protégés par un verrou unique ; le LRU est
maintenu en O(1) via ``OrderedDict``. L'expiration (TTL) est paresseuse :
les entrées périmées ne sont purgées qu'à l'accès (ou lors d'un ``stats()``).
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger("thinktuning.core.prediction_result_cache")


class PredictionResultCache:
    """Cache de résultats indexé par une clé calculée depuis l'entrée.

    Args:
        maxsize: nombre maximum d'entrées avant éviction LRU (> 0).
        ttl_seconds: durée de vie d'une entrée en secondes (0 = jamais).
    """

    def __init__(self, maxsize: int = 1024, ttl_seconds: float = 300.0) -> None:
        if maxsize <= 0:
            raise ValueError("maxsize doit être strictement positif")
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds ne peut pas être négatif")
        self._maxsize = maxsize
        self._ttl = ttl_seconds
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0

    @staticmethod
    def make_key(*parts: str) -> str:
        """Clé stable (SHA-256) pour un ensemble de fragments de texte.

        Ex. ``make_key(classifier_name, text)``. Les fragments sont
        normalisés (casse et espaces) pour maximiser les collisions utiles :
        ``"Bonjour"`` et ``" bonjour "`` partagent la même entrée de cache.
        """
        normalized = "\x1f".join(p.strip().casefold() for p in parts if p)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Any | None:
        """Valeur associée à ``key``, ou None en cas de miss / expiration."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self._misses += 1
                return None
            ts, value = entry
            if self._ttl > 0 and time.monotonic() - ts > self._ttl:
                del self._data[key]
                self._misses += 1
                self._evictions += 1
                return None
            self._hits += 1
            self._data.move_to_end(key)  # LRU : rafraîchit l'ordre d'accès
            return value

    def set(self, key: str, value: Any) -> None:
        """Insère (ou remplace) une entrée, en évictant LRU si nécessaire."""
        with self._lock:
            if key in self._data:
                self._data[key] = (time.monotonic(), value)
                self._data.move_to_end(key)
                return
            self._data[key] = (time.monotonic(), value)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)
                self._evictions += 1

    def clear(self) -> None:
        """Vide le cache et réinitialise les compteurs."""
        with self._lock:
            self._data.clear()
            self._hits = 0
            self._misses = 0
            self._evictions = 0

    def stats(self) -> dict[str, Any]:
        """Instantané pour le monitoring (taille, hit-rate, évictions)."""
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._data),
                "maxsize": self._maxsize,
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total, 4) if total else 0.0,
                "evictions": self._evictions,
                "ttl_seconds": self._ttl,
            }
