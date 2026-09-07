# project/core/job_store.py

import os
import json
import sqlite3
import threading
import time
from typing import Dict

from core.models import TrainJob

JOB_STORE_PATH = os.getenv("JOB_STORE_PATH", os.path.join("experiments", "jobs.db"))


class PersistentJobStore(dict):
    def __init__(self, path: str = JOB_STORE_PATH):
        super().__init__()
        self.path = path
        # Les jobs sont écrits par les threads workers (train/pipeline) ET par
        # les threads de requêtes API (statut, cancel) : toute écriture est
        # verrouillée pour garantir la cohérence dict <-> SQLite.
        self._write_lock = threading.RLock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._ensure_db()
        self._refresh_from_db()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30.0)

    def _ensure_db(self):
        conn = self._connect()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        # Index sur updated_at pour accélérer les requêtes de listing/pagination.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_updated_at ON jobs(updated_at)"
        )
        # SCRUM-73 : métriques d'entraînement par epoch (loss / F1 / accuracy)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS train_metrics (
                job_id TEXT NOT NULL,
                epoch INTEGER NOT NULL,
                loss REAL,
                f1_macro REAL,
                accuracy REAL,
                updated_at REAL NOT NULL,
                PRIMARY KEY (job_id, epoch)
            )
        """)
        # SCRUM-34 : planifications d'entraînements récurrents (POST /train/schedule)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
                schedule_id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                updated_at REAL NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # SCRUM-34 : planifications récurrentes
    # ------------------------------------------------------------------
    def save_schedule(self, schedule: dict):
        """Persiste (ou met à jour) une planification récurrente.

        ``schedule`` est un dict serializable (payload d'un ``ScheduledJob``).
        """
        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT INTO scheduled_jobs (schedule_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(schedule_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
                """,
                (
                    schedule["schedule_id"],
                    json.dumps(schedule),
                    time.time(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_schedules(self):
        """Renvoie toutes les planifications persistées, triées par création."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT payload FROM scheduled_jobs ORDER BY updated_at ASC"
            ).fetchall()
        finally:
            conn.close()
        return [json.loads(payload) for (payload,) in rows]

    def get_schedule(self, schedule_id: str):
        """Renvoie la planification *schedule_id* ou None."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT payload FROM scheduled_jobs WHERE schedule_id = ?",
                (schedule_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return json.loads(row[0])

    def delete_schedule(self, schedule_id: str) -> bool:
        """Supprime une planification. Renvoie True si elle existait."""
        conn = self._connect()
        try:
            cur = conn.execute(
                "DELETE FROM scheduled_jobs WHERE schedule_id = ?", (schedule_id,)
            )
            conn.commit()
        finally:
            conn.close()
        return cur.rowcount > 0

    def _serialize_job(self, job: TrainJob) -> str:
        payload = job.model_dump()
        payload["status"] = job.status.value
        return json.dumps(payload)

    def _refresh_from_db(self):
        conn = self._connect()
        rows = conn.execute("SELECT job_id, payload FROM jobs").fetchall()
        conn.close()

        super().clear()
        for job_id, payload in rows:
            data = json.loads(payload)
            super().__setitem__(job_id, TrainJob(**data))

    def __setitem__(self, key, value):
        if not isinstance(value, TrainJob):
            raise TypeError("PersistentJobStore accepts only TrainJob instances.")

        payload = self._serialize_job(value)
        with self._write_lock:
            conn = self._connect()
            conn.execute("""
                INSERT INTO jobs (job_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    payload = excluded.payload,
                    updated_at = excluded.updated_at
            """, (key, payload, time.time()))
            conn.commit()
            conn.close()

            super().__setitem__(key, value)

    def get(self, key, default=None):
        try:
            return super().__getitem__(key)
        except KeyError:
            with self._write_lock:
                # Re-vérifie sous verrou : un worker a pu insérer le job
                # entre l' KeyError et la prise du verrou.
                if key in self:
                    return self[key]
                conn = self._connect()
                row = conn.execute("SELECT payload FROM jobs WHERE job_id = ?", (key,)).fetchone()
                conn.close()
                if row is None:
                    return default
                data = json.loads(row[0])
                job = TrainJob(**data)
                super().__setitem__(key, job)
                return job

    def save_epoch_metrics(self, job_id: str, records):
        """Persiste les métriques d'entraînement par epoch (SCRUM-73).

        Args:
            job_id: Identifiant du job d'entraînement.
            records: Liste de dicts {epoch, accuracy, f1_macro[, loss]}.
        """
        if not records:
            return
        now = time.time()
        conn = self._connect()
        try:
            conn.executemany(
                """
                INSERT INTO train_metrics (job_id, epoch, loss, f1_macro, accuracy, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, epoch) DO UPDATE SET
                    loss = excluded.loss,
                    f1_macro = excluded.f1_macro,
                    accuracy = excluded.accuracy,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        job_id,
                        int(rec.get("epoch", 0)),
                        float(rec["loss"]) if rec.get("loss") is not None else None,
                        float(rec["f1_macro"]) if rec.get("f1_macro") is not None else None,
                        float(rec["accuracy"]) if rec.get("accuracy") is not None else None,
                        now,
                    )
                    for rec in records
                ],
            )
            conn.commit()
        finally:
            conn.close()

    def get_job_metrics(self, job_id: str):
        """Renvoie les métriques par epoch d'un job, triées par epoch croissant."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT job_id, epoch, loss, f1_macro, accuracy
                FROM train_metrics WHERE job_id = ? ORDER BY epoch ASC
                """,
                (job_id,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {"job_id": jid, "epoch": ep, "loss": loss, "f1_macro": f1, "accuracy": acc}
            for jid, ep, loss, f1, acc in rows
        ]

    def list_jobs(self, status=None, kind=None, limit=100, offset=0):
        """Renvoie les jobs paginés, filtrés par *status* / *kind* et triés par
        ``started_at DESC`` (les jobs non-démarrés arrivent en dernier).

        La requête SQL exploite l'index ``idx_jobs_updated_at`` et utilise
        ``json_extract`` pour filtrer / trier directement dans SQLite, évitant
        de charger l'ensemble des payloads en mémoire.

        Args:
            status: Valeur texte du status (ex. ``"completed"``) ou ``None``
                pour désactiver le filtre.
            kind: Type de job (``"train"``, ``"pipeline"``, ``"intent"`` —
                SCRUM-95) ou ``None`` pour désactiver le filtre. Les payloads
                antérieurs au champ ``kind`` (NULL) ne correspondent à aucun
                filtre explicite, donc ``kind=None`` reste rétro-compatible.
            limit: Nombre maximum de résultats (``>= 1``).
            offset: Nombre de résultats à ignorer (``>= 0``).

        Returns:
            Tuple ``(items, total)`` où *items* est une liste de ``TrainJob``
            et *total* le nombre total de jobs correspondant au filtre.
        """
        limit = max(1, limit)
        offset = max(0, offset)

        where_clauses = []
        params = []
        if status:
            where_clauses.append("json_extract(payload, '$.status') = ?")
            params.append(status)
        if kind:
            where_clauses.append("json_extract(payload, '$.kind') = ?")
            params.append(kind)
        where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        count_sql = f"SELECT COUNT(*) FROM jobs {where}"
        items_sql = (
            f"SELECT job_id, payload FROM jobs {where} "
            "ORDER BY json_extract(payload, '$.started_at') DESC "
            "LIMIT ? OFFSET ?"
        )
        items_params = params + [limit, offset]

        conn = self._connect()
        try:
            total = conn.execute(count_sql, params).fetchone()[0]
            rows = conn.execute(items_sql, items_params).fetchall()
        finally:
            conn.close()

        items = []
        for _job_id, payload in rows:
            data = json.loads(payload)
            items.append(TrainJob(**data))

        return items, total

    # ------------------------------------------------------------------
    # Rétention (cleanup_old_jobs)
    # ------------------------------------------------------------------
    def update_job_timestamp(self, job_id: str, timestamp: float) -> bool:
        """Force ``updated_at`` d'un job (outillage/tests de rétention).

        Renvoie True si le job existait.
        """
        with self._write_lock:
            conn = self._connect()
            cur = conn.execute(
                "UPDATE jobs SET updated_at = ? WHERE job_id = ?", (timestamp, job_id)
            )
            conn.commit()
            conn.close()
        return cur.rowcount > 0

    def remove_job(self, job_id: str) -> bool:
        """Supprime un job de la mémoire ET du SQLite (avec ses métriques)."""
        with self._write_lock:
            conn = self._connect()
            cur = conn.execute("DELETE FROM jobs WHERE job_id = ?", (job_id,))
            conn.execute("DELETE FROM train_metrics WHERE job_id = ?", (job_id,))
            conn.commit()
            conn.close()
            super().pop(job_id, None)
        if cur.rowcount > 0:
            # Purge aussi le buffer de logs du job (job_logs, cf. rétention).
            try:
                from core.job_logs import reset_job_logs

                reset_job_logs(job_id)
            except Exception:
                pass
        return cur.rowcount > 0

    def cleanup_old_jobs(self, max_age_days: int = 30, dry_run: bool = False) -> dict:
        """Purge les jobs terminés plus vieux que ``max_age_days`` jours.

        Utilise l'index ``idx_jobs_updated_at`` : pas de scan complet.
        Ne touche jamais aux jobs actifs (pending/running).
        """
        cutoff = time.time() - max_age_days * 86400
        placeholders = ", ".join("?" for _ in TERMINAL_STATUSES)
        conn = self._connect()
        try:
            rows = conn.execute(
                f"""
                SELECT job_id FROM jobs
                WHERE updated_at < ?
                  AND json_extract(payload, '$.status') IN ({placeholders})
                """,
                (cutoff, *TERMINAL_STATUSES),
            ).fetchall()
        finally:
            conn.close()

        job_ids = [row[0] for row in rows]
        if not dry_run:
            for job_id in job_ids:
                self.remove_job(job_id)
        return {"deleted": 0 if dry_run else len(job_ids), "job_ids": job_ids}


_store = PersistentJobStore()


def get_job_store() -> PersistentJobStore:
    return _store


# ---------------------------------------------------------------------------
# Rétention : purge des jobs terminés obsolètes (CLI cleanup_old_jobs.py,
# appelable aussi programmatiquement). Les jobs actifs (pending/running) ne
# sont JAMAIS supprimés.
# ---------------------------------------------------------------------------
TERMINAL_STATUSES = ("completed", "failed", "cancelled")


def cleanup_old_jobs(
    max_age_days: int = 30,
    dry_run: bool = False,
    db_path: str | None = None,
) -> dict:
    """Supprime les jobs TERMINAUX datant de plus de ``max_age_days`` jours.

    Args:
        max_age_days: Âge maximal de rétention des jobs terminés.
        dry_run: Si True, liste les jobs qui seraient supprimés sans rien faire.
        db_path: Chemin SQLite optionnel (défaut : store singleton).

    Returns:
        {"deleted": int, "job_ids": list[str]} — ``deleted`` vaut 0 en dry-run.

    Note :
        Avec ``db_path`` (hors singleton), une instance dédiée est créée ; les
        caches mémoire d'autres instances déjà instanciées ne sont PAS
        synchronisés (seul SQLite fait foi entre instances).
    """
    store = PersistentJobStore(path=db_path) if db_path else get_job_store()
    return store.cleanup_old_jobs(max_age_days=max_age_days, dry_run=dry_run)

