"""
Tests POST /train/schedule — planification récurrente (SCRUM-34).

Couvre :
- POST /train/schedule avec cron ou intervalle -> 202 + persistance SQLite
- Validation : ni l'un ni l'autre / les deux -> 422, cron invalide -> 422
- GET /train/schedules (next_run_at renseigné), DELETE /train/schedules/{id}
- Survie au redémarrage (définitions rechargées depuis PersistentJobStore)
"""
import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("JOB_STORE_PATH", os.path.join("tests", "tmp", "sched_jobs.db"))
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from api.main import app  # noqa: E402
from core import scheduler as scheduler_mod  # noqa: E402
from core.job_store import get_job_store  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}

TRAIN_PAYLOAD = {"max_per_lang": 10, "epochs": 1, "use_back_translation": False}


@pytest.fixture()
def client():
    # Scheduler partagé entre les tests (singleton) : on nettoie les
    # planifications après chaque test.
    yield TestClient(app)
    scheduler = scheduler_mod.get_scheduler()
    if scheduler.running:
        for job in list(scheduler.get_jobs()):
            scheduler.remove_job(job.id)


def test_schedule_with_interval(client):
    resp = client.post(
        "/train/schedule",
        json={"train": TRAIN_PAYLOAD, "interval_minutes": 60},
        headers=HEADERS,
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["trigger"] == "interval"
    assert body["interval_minutes"] == 60
    assert body["cron"] is None
    assert body["next_run_at"] is not None
    assert body["status"] == "scheduled"

    # Persisté dans le SQLite existant (table scheduled_jobs).
    stored = get_job_store().get_schedule(body["schedule_id"])
    assert stored is not None
    assert stored["train_request"]["max_per_lang"] == 10


def test_schedule_with_cron(client):
    resp = client.post(
        "/train/schedule",
        json={"train": TRAIN_PAYLOAD, "cron": "0 2 * * *"},
        headers=HEADERS,
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["trigger"] == "cron"
    assert body["cron"] == "0 2 * * *"
    assert body["next_run_at"] is not None


def test_schedule_requires_exactly_one_trigger(client):
    # Ni l'un ni l'autre -> 422
    resp = client.post("/train/schedule", json={"train": TRAIN_PAYLOAD}, headers=HEADERS)
    assert resp.status_code == 422

    # Les deux -> 422
    resp = client.post(
        "/train/schedule",
        json={"train": TRAIN_PAYLOAD, "cron": "0 2 * * *", "interval_minutes": 30},
        headers=HEADERS,
    )
    assert resp.status_code == 422

    # Cron malformé -> 422
    resp = client.post(
        "/train/schedule",
        json={"train": TRAIN_PAYLOAD, "cron": "tous les jours"},
        headers=HEADERS,
    )
    assert resp.status_code == 422


def test_list_schedules(client):
    resp = client.post(
        "/train/schedule",
        json={"train": TRAIN_PAYLOAD, "interval_minutes": 120},
        headers=HEADERS,
    )
    schedule_id = resp.json()["schedule_id"]

    resp = client.get("/train/schedules", headers=HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    ids = [item["schedule_id"] for item in body["items"]]
    assert schedule_id in ids
    item = next(i for i in body["items"] if i["schedule_id"] == schedule_id)
    assert item["next_run_at"] is not None


def test_delete_schedule(client):
    resp = client.post(
        "/train/schedule",
        json={"train": TRAIN_PAYLOAD, "interval_minutes": 60},
        headers=HEADERS,
    )
    schedule_id = resp.json()["schedule_id"]

    resp = client.delete(f"/train/schedules/{schedule_id}", headers=HEADERS)
    assert resp.status_code == 204
    assert get_job_store().get_schedule(schedule_id) is None

    # Deuxième suppression -> 404
    resp = client.delete(f"/train/schedules/{schedule_id}", headers=HEADERS)
    assert resp.status_code == 404


def test_schedules_survive_restart(client):
    resp = client.post(
        "/train/schedule",
        json={"train": TRAIN_PAYLOAD, "cron": "30 3 * * 1"},
        headers=HEADERS,
    )
    schedule_id = resp.json()["schedule_id"]

    # Simule un redémarrage : nouveau scheduler qui recharge depuis SQLite.
    old = scheduler_mod.get_scheduler()
    old.shutdown(wait=False)
    scheduler_mod._scheduler = None  # noqa: SLF001
    scheduler_mod.ensure_scheduler_started()

    new = scheduler_mod.get_scheduler()
    assert new.running
    assert new.get_job(schedule_id) is not None
    assert get_job_store().get_schedule(schedule_id) is not None
