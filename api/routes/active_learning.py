# project/api/routes/active_learning.py

"""Routes du cycle Active Learning (SCRUM-55).

  POST /active_learning          -> exemples les plus incertains (conf ~ 1/3)
  POST /annotate                 -> stocke une correction manuelle
  GET  /annotate/list            -> annotations enregistrees
  POST /annotate/export          -> export CSV de review
  POST /annotate/merge           -> fusion des annotations dans le dataset
  POST /active_learning/cycle    -> cycle complet (202 + job asynchrone)
  GET  /active_learning/cycle/status/{job_id}
"""

import json
import os
import threading
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException

from api.dependencies.auth import require_api_key
from core.annotation_store import get_annotation_store
from core.cycle_runner import run_cycle
from core.job_store import get_job_store
from core.models import (
    ActiveLearningRequest,
    AnnotateListResponse,
    AnnotateRequest,
    CycleRequest,
    JobStatus,
    MergeAnnotationsResponse,
    TrainJob,
    TrainRequest,
)

router = APIRouter(tags=["Active Learning"])

_jobs_lock = threading.Lock()


def _load_texts(dataset_path: Optional[str], texts: Optional[List[str]]) -> List[str]:
    if texts:
        return [str(t).strip() for t in texts if str(t).strip()]
    path = dataset_path or os.path.join("data", "train_enriched.jsonl")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"Dataset introuvable : {path}")
    loaded: List[str] = []
    if path.endswith(".csv"):
        import csv

        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                value = (row.get("text") or "").strip()
                if value:
                    loaded.append(value)
    else:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                value = str(rec.get("text", "")).strip()
                if value:
                    loaded.append(value)
    return loaded


@router.post("/active_learning", status_code=200)
def select_examples(req: ActiveLearningRequest, _: bool = Depends(require_api_key)):
    """Retourne les exemples tries par incertitude decroissante.

    Format strict par exemple : {text, predicted_label, confidence, uncertainty}.
    """
    from active_learning import select_uncertain_examples

    texts = _load_texts(req.dataset_path, req.texts)
    if not texts:
        raise HTTPException(status_code=400, detail="Aucun texte a scorer.")
    try:
        records = select_uncertain_examples(
            texts,
            model_path=req.model_version,
            batch_size=req.batch_size,
            top_n=req.top_n,
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"total": len(records), "items": records}


@router.post("/annotate", status_code=200)
def annotate(req: AnnotateRequest, _: bool = Depends(require_api_key)):
    """Enregistre une correction manuelle (dedupliquee par texte normalise)."""
    store = get_annotation_store()
    try:
        record = store.annotate(req.text, req.label, force=req.force)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return record


@router.get("/annotate/list", response_model=AnnotateListResponse)
def list_annotations(limit: int = 100, offset: int = 0, _: bool = Depends(require_api_key)):
    store = get_annotation_store()
    return AnnotateListResponse(total=store.count(), items=store.list(limit=limit, offset=offset))


@router.post("/annotate/export")
def export_annotations(output_path: Optional[str] = None, _: bool = Depends(require_api_key)):
    store = get_annotation_store()
    path = store.export_review_csv(output_path)
    return {"path": path, "count": store.count()}


@router.post("/annotate/merge", response_model=MergeAnnotationsResponse)
def merge_annotations(output_path: Optional[str] = None, _: bool = Depends(require_api_key)):
    store = get_annotation_store()
    try:
        stats = store.merge_annotations(output_path=output_path)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Echec de la fusion : {exc}")
    return MergeAnnotationsResponse(stats=stats)


@router.post("/active_learning/cycle", response_model=TrainJob, status_code=202)
def start_cycle(req: CycleRequest, _: bool = Depends(require_api_key)):
    """Lance le cycle complet : merge -> retrain -> activation conditionnelle."""
    job_id = str(uuid.uuid4())
    job = TrainJob(job_id=job_id, status=JobStatus.PENDING)
    train_req = req.train or TrainRequest()
    store = get_job_store()
    with _jobs_lock:
        store[job_id] = job
    thread = threading.Thread(
        target=run_cycle, args=(job_id, train_req, req.auto_activate), daemon=True
    )
    thread.start()
    return job


@router.get("/active_learning/cycle/status/{job_id}", response_model=TrainJob)
def cycle_status(job_id: str, _: bool = Depends(require_api_key)):
    job = get_job_store().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_id introuvable")
    return job
