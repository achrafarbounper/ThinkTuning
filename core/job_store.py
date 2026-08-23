# project/core/job_store.py

import os
import json
import sqlite3
import time
from typing import Dict

from core.models import TrainJob

JOB_STORE_PATH = os.getenv("JOB_STORE_PATH", os.path.join("experiments", "jobs.db"))


class PersistentJobStore(dict):
    def __init__(self, path: str = JOB_STORE_PATH):
        super().__init__()
        self.path = path
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
        conn.commit()
        conn.close()

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
            conn = self._connect()
            row = conn.execute("SELECT payload FROM jobs WHERE job_id = ?", (key,)).fetchone()
            conn.close()
            if row is None:
                return default
            data = json.loads(row[0])
            job = TrainJob(**data)
            super().__setitem__(key, job)
            return job


_store = PersistentJobStore()


def get_job_store() -> PersistentJobStore:
    return _store
