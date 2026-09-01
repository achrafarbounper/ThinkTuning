# project/api/routes/train.py

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
import asyncio
import json
import os
import threading
import time
import uuid

from api.dependencies.auth import require_api_key, _get_api_key
from core.job_store import get_job_store
from core.training_events import (
    get_training_events_source,
    ACTIVE_POLL_SECONDS,
    STALL_MINUTES,
)
from core.trainer_runner import run_training, cancel_training
from core import scheduler as schedule_manager
from core.models import (
    TrainRequest,
    TrainJob,
    JobStatus,
    JobListResponse,
    EpochMetric,
    TrainHistoryResponse,
    ScheduleRequest,
    ScheduledJob,
    ScheduleListResponse,
)

router = APIRouter(prefix="/train", tags=["Training"])

_jobs_lock = threading.Lock()
_job_cancel_events = {}


@router.post("", response_model=TrainJob, status_code=202)
def start_training(req: TrainRequest, _: bool = Depends(require_api_key)):
    job_id = str(uuid.uuid4())
    job = TrainJob(job_id=job_id, status=JobStatus.PENDING)

    store = get_job_store()
    with _jobs_lock:
        store[job_id] = job
        _job_cancel_events.setdefault(job_id, threading.Event())

    thread = threading.Thread(target=run_training, args=(job_id, req), daemon=True)
    thread.start()

    return job


@router.get("/status/{job_id}", response_model=TrainJob)
def get_training_status(job_id: str, _: bool = Depends(require_api_key)):
    store = get_job_store()
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_id introuvable")
    return job


@router.get("/history/{job_id}", response_model=TrainHistoryResponse)
def get_training_history(job_id: str, _: bool = Depends(require_api_key)):
    """Historique des métriques d'entraînement (loss / F1 / accuracy) par epoch.

    SCRUM-73 : les métriques sont persistées dans le SQLite existant
    (table ``train_metrics``) pendant l'entraînement. Un job connu mais
    encore sans métriques renvoie une liste vide.
    """
    store = get_job_store()
    if store.get(job_id) is None:
        raise HTTPException(status_code=404, detail="job_id introuvable")
    rows = store.get_job_metrics(job_id)
    return TrainHistoryResponse(
        job_id=job_id,
        epochs=[EpochMetric(**row) for row in rows],
    )


@router.post("/cancel/{job_id}", response_model=TrainJob)
def cancel_training_endpoint(job_id: str, _: bool = Depends(require_api_key)):
    return cancel_training(job_id)

@router.get("/jobs", response_model=JobListResponse)
def list_training_jobs(
    status: Optional[JobStatus] = Query(
        default=None,
        description="Filtrer par status : pending, running, completed, failed, cancelled",
    ),
    limit: int = Query(default=100, ge=1, le=1000, description="Nombre max de résultats"),
    offset: int = Query(default=0, ge=0, description="Nombre de résultats à ignorer"),
    _: bool = Depends(require_api_key),
):
    """Liste paginée et filtrée des jobs d'entraînement.

    - Query params : ``?status=completed&limit=20&offset=0``
    - Tri par ``started_at DESC`` par défaut.
    - Réponse : ``{ total, items, limit, offset }``.
    """
    store = get_job_store()
    status_value = status.value if status else None
    items, total = store.list_jobs(status=status_value, limit=limit, offset=offset)
    return JobListResponse(
        total=total,
        items=items,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# SCRUM-34 : planification récurrente d'entraînements (cron-like, APScheduler)
# ---------------------------------------------------------------------------

@router.post("/schedule", response_model=ScheduledJob, status_code=202)
def schedule_training(req: ScheduleRequest, _: bool = Depends(require_api_key)):
    """Programme un entraînement récurrent.

    - ``cron`` : expression cron à 5 champs, ex. ``"0 2 * * *"`` (chaque jour à 2h).
    - ``interval_minutes`` : intervalle en minutes, ex. ``60`` (toutes les heures).

    Exactement l'un des deux doit être fourni. La planification est persistée
    dans le SQLite existant (table ``scheduled_jobs``) et rechargée au démarrage.
    """
    try:
        schedule = schedule_manager.create_schedule(
            cron=req.cron,
            interval_minutes=req.interval_minutes,
            train_request=req.train.model_dump(),
        )
    except ValueError as err:
        raise HTTPException(status_code=422, detail=str(err)) from err
    return ScheduledJob(**schedule)


@router.get("/schedules", response_model=ScheduleListResponse)
def list_training_schedules(_: bool = Depends(require_api_key)):
    """Liste les planifications d'entraînement actives avec leur prochaine exécution."""
    items = schedule_manager.list_schedules()
    return ScheduleListResponse(total=len(items), items=[ScheduledJob(**s) for s in items])


@router.delete("/schedules/{schedule_id}", status_code=204)
def delete_training_schedule(schedule_id: str, _: bool = Depends(require_api_key)):
    """Supprime une planification récurrente."""
    deleted = schedule_manager.delete_schedule(schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="schedule_id introuvable")
    return None


# ---------------------------------------------------------------------------
# WebSocket : métriques d'entraînement en temps réel (epoch par epoch)
# ---------------------------------------------------------------------------

# Auth : les navigateurs ne peuvent pas poser de header sur un WebSocket, le
# jeton est donc passé en query param `?token=` (même convention que
# /api/agent/ws). Jeton DÉDIÉ au dashboard via DASHBOARD_WS_TOKEN : ce canal
# est un usage interne au dashboard, pas un endpoint public. En dev local, on
# retombe sur la clé API pour rester simple.
def _get_dashboard_ws_token() -> str:
    return os.getenv("DASHBOARD_WS_TOKEN") or _get_api_key()


@router.websocket("/stream/{job_id}")
async def stream_training_metrics(websocket: WebSocket, job_id: str):
    """Diffuse les métriques (loss / F1 / accuracy) epoch par epoch.

    Protocole (messages JSON) :
        {"type": "step", "step": "labeling|loading_dataset|training|saving_model|..."}
        {"type": "log", "seq", "ts", "level", "step", "message"}   # logs serveur
        {"type": "progress", "job_id", "phase", "epoch", "epochs_total",
         "batch", "batches_total", "batch_pct", "global_pct",
         "rate_it_s", "eta", "steps": {<step>: {"status": ...}}}
        {"type": "epoch", "job_id", "epoch", "loss", "f1_macro", "accuracy"}
        {"type": "end", "status": "completed|failed|cancelled"}
        {"type": "stalled", "last_epoch", "minutes"}
        {"type": "error", "detail"}

    - Les epochs déjà réalisées ET les logs déjà émis sont envoyés
      immédiatement à la connexion (reconnexion en cours d'entraînement OK).
    - L'étape courante du pipeline est diffusée à la connexion puis à chaque
      changement (labeling, loading_model, training, saving_model, ...).
    - L'avancement batch-par-batch (comme tqdm) est diffusé via l'événement
      "progress" quand job.progress change (throttle ~1×/s côté worker).
    - Polling adaptatif : 0,5 s tant que le job est actif ; arrêt immédiat
      (événement "end" + fermeture propre) dès que le statut est terminal.
    - Anti-stall : si le job reste "running" SANS NOUVEL EPOCH pendant
      TRAIN_STREAM_STALL_MINUTES (défaut 5) alors qu'il est à l'étape
      "training", événement "stalled" + fermeture. Les autres étapes
      (labeling, loading_model, ...) ne déclenchent pas le stall : elles
      peuvent légitimement durer plus longtemps sans produire d'epoch.
    """
    if websocket.query_params.get("token") != _get_dashboard_ws_token():
        await websocket.close(code=1008, reason="Jeton invalide")
        return
    await websocket.accept()

    source = get_training_events_source()
    status = await source.get_status(job_id)
    if status is None:
        await websocket.send_json({"type": "error", "detail": "job_id introuvable"})
        await websocket.close(code=1008, reason="job_id introuvable")
        return

    last_epoch = 0
    last_event_time = time.time()
    last_step: Optional[str] = None
    last_log_seq = 0
    last_progress_json: Optional[str] = None
    try:
        while True:
            # Étape courante du pipeline : diffusée à la connexion puis à
            # chaque changement (labeling, loading_model, training, ...).
            # Un changement d'étape réinitialise aussi le compteur anti-stall.
            step = await source.get_step(job_id)
            if step is not None and step != last_step:
                await websocket.send_json({"type": "step", "step": step})
                last_step = step
                last_event_time = time.time()

            # Logs serveur de l'étape courante : envoyés dans l'ordre, avant
            # les autres événements. Rejeu complet à la connexion (seq=0).
            logs = await source.get_logs(job_id, last_log_seq)
            for entry in logs:
                await websocket.send_json({"type": "log", **entry})
                last_log_seq = entry["seq"]
                last_event_time = time.time()

            # Avancement batch-par-batch (comme tqdm) : diffusé quand
            # job.progress change (le worker throttle déjà à ~1×/s).
            progress = await source.get_progress(job_id)
            if progress is not None:
                progress_json = json.dumps(progress, sort_keys=True)
                if progress_json != last_progress_json:
                    await websocket.send_json(
                        {"type": "progress", "job_id": job_id, **progress}
                    )
                    last_progress_json = progress_json
                    last_event_time = time.time()

            events = await source.get_new_events(job_id, last_epoch)
            for event in events:
                await websocket.send_json(event)
                last_epoch = event["epoch"]
                last_event_time = time.time()

            status = await source.get_status(job_id)
            if status is None:
                # Job supprimé en cours de diffusion (cleanup_old_jobs) : on
                # clôture proprement après avoir envoyé ce qui existe.
                await websocket.send_json({"type": "end", "status": "unknown"})
                await websocket.close()
                return

            if source.is_terminal(status):
                await websocket.send_json({"type": "end", "status": status})
                await websocket.close()
                return

            # Anti-stall : uniquement pendant l'étape "training" — job toujours
            # actif mais plus aucun nouvel epoch. Les autres étapes (labeling,
            # loading_model, saving_model, ...) peuvent légitimement durer
            # plus de STALL_MINUTES sans produire d'epoch.
            if last_step == "training":
                stalled_minutes = (time.time() - last_event_time) / 60.0
                if stalled_minutes >= STALL_MINUTES:
                    await websocket.send_json(
                        {
                            "type": "stalled",
                            "last_epoch": last_epoch,
                            "minutes": round(stalled_minutes, 1),
                        }
                    )
                    await websocket.close()
                    return

            await asyncio.sleep(ACTIVE_POLL_SECONDS)
    except WebSocketDisconnect:
        # Déconnexion du dashboard : rien à faire, la boucle s'arrête.
        return