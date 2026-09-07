# project/core/job_logs.py

"""Capture des logs serveur par job d'entraînement (flux WebSocket).

Un ``JobLogHandler`` est attaché au logger racine une seule fois ; il capture
les records émis par le thread qui exécute ``run_training`` (via un mapping
``thread ident -> job_id``) et les empile dans un buffer circulaire par job.
Le WebSocket /train/stream/{job_id} rejoue ensuite ces lignes au dashboard
(événements ``{"type": "log", "seq", "ts", "level", "step", "message"}``).

Limites connues :
- Buffer EN MÉMOIRE : les logs sont perdus au redémarrage de l'API (cohérent
  avec le pattern actuel ; une table SQLite pourrait remplacer les buffers
  derrière la même interface plus tard).
- Multi-workers (uvicorn > 1 worker) : seuls les logs du worker qui exécute
  le job sont capturés (même limite que le store SQLite partagé).
"""

import logging
import threading
import time
from collections import deque
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Nombre max de lignes conservées par job (buffer circulaire).
MAX_LOGS_PER_JOB = 500
# Niveau minimal capturé : on évite le flux DEBUG (très verbeux) ; les
# messages utiles au dashboard sont en INFO/WARNING/ERROR.
CAPTURE_LEVEL = logging.INFO

_lock = threading.Lock()
# job_id -> deque de {"seq", "ts", "level", "step", "message"}
_buffers: Dict[str, deque] = {}
# job_id -> dernier seq attribué
_sequences: Dict[str, int] = {}
# threading.get_ident() -> job_id (un job = un thread daemon dédié)
_thread_jobs: Dict[int, str] = {}
# threading.get_ident() -> étape courante du pipeline pour ce job
_thread_steps: Dict[int, Optional[str]] = {}


class JobLogHandler(logging.Handler):
    """Handler qui route les records du thread du job vers son buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        ident = threading.get_ident()
        with _lock:
            job_id = _thread_jobs.get(ident)
            step = _thread_steps.get(ident)
        if job_id is None:
            return
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        _append(job_id, message, level=record.levelname, step=step)


_handler: Optional[JobLogHandler] = None


def ensure_job_log_handler() -> None:
    """Installe le handler sur le logger racine (une seule fois).

    Le handler ne capture que les records de niveau >= INFO (le DEBUG reste
    dans les logs console/terminal, trop verbeux pour le dashboard).
    """
    global _handler
    if _handler is not None:
        return
    handler = JobLogHandler()
    handler.setLevel(CAPTURE_LEVEL)
    root = logging.getLogger()
    # Éviter les doublons (rechargement de module, tests, ...).
    for existing in root.handlers:
        if isinstance(existing, JobLogHandler):
            _handler = existing
            return
    root.addHandler(handler)
    _handler = handler


def attach_job_logging(job_id: str) -> None:
    """Associe le thread courant au job *job_id* (à appeler au début de
    ``run_training``). Les records émis par ce thread sont alors capturés."""
    ensure_job_log_handler()
    ident = threading.get_ident()
    with _lock:
        _thread_jobs[ident] = job_id
        _thread_steps[ident] = None
        _buffers.setdefault(job_id, deque(maxlen=MAX_LOGS_PER_JOB))
        _sequences.setdefault(job_id, 0)


def set_job_log_step(step: Optional[str]) -> None:
    """Met à jour l'étape courante du pipeline pour le job du thread courant.

    Les lignes émises après cet appel seront taguées avec cette étape.
    """
    ident = threading.get_ident()
    with _lock:
        if ident in _thread_jobs:
            _thread_steps[ident] = step


def detach_job_logging() -> None:
    """Dissocie le thread courant de son job (à appeler dans un ``finally``)."""
    ident = threading.get_ident()
    with _lock:
        _thread_jobs.pop(ident, None)
        _thread_steps.pop(ident, None)


def _append(job_id: str, message: str, level: str, step: Optional[str]) -> None:
    with _lock:
        buf = _buffers.get(job_id)
        if buf is None:
            return
        seq = _sequences.get(job_id, 0) + 1
        _sequences[job_id] = seq
        buf.append(
            {
                "seq": seq,
                "ts": round(time.time(), 3),
                "level": level,
                "step": step,
                "message": message,
            }
        )


def get_logs(job_id: str, since_seq: int = 0) -> List[dict]:
    """Renvoie les lignes de log du job avec ``seq > since_seq``, triées.

    Rejeu à la connexion du WebSocket : ``since_seq=0`` renvoie tout l'historique.
    """
    with _lock:
        buf = _buffers.get(job_id)
        if not buf:
            return []
        return [dict(entry) for entry in buf if entry["seq"] > since_seq]


def reset_job_logs(job_id: str) -> None:
    """Purge le buffer du job (fin de rétention / cleanup_old_jobs)."""
    with _lock:
        _buffers.pop(job_id, None)
        _sequences.pop(job_id, None)
