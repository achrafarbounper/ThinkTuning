"""DynamicBatcher : regroupement adaptatif des prédictions en lots.

But : sous charge burst, traiter les textes un par un gaspille le coût du
prétraitement (tokenisation incluse). Ce batcher accumule les demandes pendant
une courte fenêtre (``window_seconds``) ou jusqu'à ``max_batch_size``, puis
exécute UNE inférence batch et distribue les résultats aux demandeurs.

Caractéristiques :
  - thread-safe (producteurs multiples) ; un seul thread worker exécute
    l'inférence, ce qui sérialise naturellement l'accès au modèle ;
  - backpressure : ``submit`` attend que la file se libère si ``max_queue``
    est atteinte (aucune donnée perdue) ;
  - erreurs : si l'inférence lève, TOUS les demandeurs du lot reçoivent la
    même exception ;
  - métriques : nombre de lots, taille moyenne, profondeur de file.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("thinktuning.core.dynamic_batcher")

_Result = Any
_Item = tuple[int, str]  # (seq, texte)


class _PendingItem:
    """Demande en attente : séquence, texte, événement de libération."""

    __slots__ = ("seq", "text", "done", "result", "error")

    def __init__(self, seq: int, text: str) -> None:
        self.seq = seq
        self.text = text
        self.done = threading.Event()
        self.result: Any | None = None
        self.error: BaseException | None = None


class DynamicBatcher:
    """Batcher à fenêtre fixe et taille de lot maximale.

    Args:
        inference_fn: appelable acceptant une liste de textes et retournant une
            liste de résultats (ordre aligné ; une incohérence de longueur est
            une erreur, propagée aux demandeurs).
        max_batch_size: nombre de textes par lot (départ immédiat si atteint).
        window_seconds: durée d'accumulation après l'arrivée du 1er texte.
        max_queue: nombre maximal de demandes en attente avant backpressure.
        name: préfixe du thread worker (debug).
    """

    def __init__(
        self,
        inference_fn: Callable[[list[str]], list[_Result]],
        *,
        max_batch_size: int = 32,
        window_seconds: float = 0.02,
        max_queue: int = 512,
        name: str = "dynamic-batcher",
    ) -> None:
        if max_batch_size < 1:
            raise ValueError("max_batch_size doit être >= 1")
        if window_seconds < 0:
            raise ValueError("window_seconds ne peut pas être négatif")
        if max_queue < 1:
            raise ValueError("max_queue doit être >= 1")

        self._inference_fn = inference_fn
        self._max_batch_size = max_batch_size
        self._window_seconds = window_seconds
        self._max_queue = max_queue
        self._name = name

        self._lock = threading.Lock()
        self._has_items = threading.Condition(self._lock)
        self._pending: deque[_Item] = deque()
        self._items: dict[int, _PendingItem] = {}
        self._first_arrival: float | None = None

        self._next_seq = 0
        self._stopped = False

        self._batches = 0
        self._processed = 0
        self._errors = 0

        self._worker = threading.Thread(target=self._worker_loop, name=name, daemon=True)
        self._worker.start()

    # -- API publique --------------------------------------------------------

    def submit(self, text: str) -> Any:
        """Soumet un texte et bloque jusqu'au résultat (ou à l'exception)."""
        with self._has_items:
            while len(self._pending) >= self._max_queue and not self._stopped:
                self._has_items.wait()  # backpressure : la file se libère
            if self._stopped:
                raise RuntimeError(f"{self._name} : batcher arrêté")
            seq = self._next_seq
            self._next_seq += 1
            item = _PendingItem(seq, text)
            self._items[seq] = item
            self._pending.append((seq, text))
            if self._first_arrival is None:
                self._first_arrival = time.perf_counter()
            self._has_items.notify_all()  # réveille le worker en attente

        item.done.wait()
        if item.error is not None:
            raise item.error
        return item.result

    def stats(self) -> dict[str, Any]:
        """Instantané pour le monitoring (lots, taille moyenne, attente)."""
        with self._has_items:
            avg = round(self._processed / self._batches, 3) if self._batches else 0.0
            return {
                "batches": self._batches,
                "processed": self._processed,
                "avg_batch_size": avg,
                "pending": len(self._pending),
                "waiting": len(self._items),
                "errors": self._errors,
            }

    def stop(self, wait: bool = True, timeout: float | None = None) -> None:
        """Stoppe le worker ; les demandeurs encore bloqués reçoivent une erreur."""
        with self._has_items:
            self._stopped = True
            self._pending.clear()  # plus rien à moissonner : le worker sortira
            for seq in list(self._items):
                item = self._items.pop(seq)
                item.error = RuntimeError(f"{self._name} : batcher arrêté")
                item.done.set()
            self._has_items.notify_all()
        if wait:
            self._worker.join(timeout=timeout)

    def __enter__(self) -> DynamicBatcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # -- Worker ----------------------------------------------------------------

    def _worker_loop(self) -> None:
        while True:
            with self._has_items:
                while not self._pending and not self._stopped:
                    self._has_items.wait()
                if self._stopped and not self._pending:
                    break

                # Fenêtre d'accumulation : différée tant que la fenêtre court
                # (on sort dès que le lot est plein ou l'arrêt demandé).
                if self._window_seconds > 0 and not self._stopped:
                    first = self._first_arrival or time.perf_counter()
                    deadline = first + self._window_seconds
                    now = time.perf_counter()
                    while now < deadline:
                        if len(self._pending) >= self._max_batch_size or self._stopped:
                            break
                        self._has_items.wait(timeout=deadline - now)
                        now = time.perf_counter()

                seqs: list[int] = []
                texts: list[str] = []
                while self._pending and len(seqs) < self._max_batch_size:
                    seq, text = self._pending.popleft()
                    seqs.append(seq)
                    texts.append(text)
                if not self._pending:
                    self._first_arrival = None
                if not seqs:
                    continue  # défensif : rien à inférer
                self._batches += 1
                self._processed += len(seqs)

            # Inférence HORS verrou : rien ne bloque les producteurs pendant
            # l'exécution du batch.
            error: BaseException | None = None
            results: list[Any] | None = None
            try:
                results = self._inference_fn(texts)
                returned = len(results) if isinstance(results, list) else -1
                if results is None or returned != len(texts):
                    raise RuntimeError(
                        f"{self._name} : l'inférence a retourné {returned} "
                        f"résultat(s) pour {len(texts)} texte(s)"
                    )
            except BaseException as exc:  # noqa: BLE001 - propagée aux demandeurs
                error = exc

            with self._has_items:
                if error is not None:
                    self._errors += len(seqs)
                    for seq in seqs:
                        item = self._items.pop(seq, None)
                        if item is None:
                            continue
                        item.error = error
                        item.done.set()
                else:
                    for seq, result in zip(seqs, results, strict=True):
                        item = self._items.pop(seq, None)
                        if item is None:
                            continue
                        item.result = result
                        item.done.set()
                self._has_items.notify_all()
