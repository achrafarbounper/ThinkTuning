"""Outils métier ThinkTuning pour l'agent IA : jobs d'entraînement, prédiction
de sentiment, profil de dataset et versions de modèles.

Donne à l'agent une vue STRUCTURÉE de la plateforme sans lui faire écrire du
SQL brut ni importer torch inutilement :
    - job_list / job_get : experiments/jobs.db en lecture seule stricte ;
    - predict_sentiment  : prédicteur courant via le cache core/predictor_cache
      (import paresseux : tests et environnements sans modèle fonctionnent) ;
    - dataset_stats      : profil CSV/TSV/JSONL via pandas ;
    - model_versions     : scan sandbox de experiments/models (mêmes conventions
      que core/model_versioning.py) ;
    - start_training     : lance un entraînement en arrière-plan (même
      mécanique que POST /train) et rend la main immédiatement ;
    - train_model        : variante bloquante qui attend la fin du job
      (timeout borné) avant de retourner le résultat final ;
    - cancel_training / stop_training : demande l'arrêt d'un entraînement
      en cours via le cancel_event du Trainer (effectif au prochain
      point de contrôle de la boucle d'entraînement).

Sécurité : SQLite mode ro + PRAGMA query_only, chemins confinés par
safe_resolve, listes bornées, aucune variable d'environnement exposée.
"""

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from .sandbox import iso_from_timestamp, safe_resolve, truncate_output

JOBS_DB_RELATIVE = Path("experiments") / "jobs.db"
DEFAULT_JOB_LIMIT = 20

# Miroir des valeurs de core.models.JobStatus : évite un import croisé, ce
# module doit rester importable seul (runtime ia/tools comme tests tools/*).
VALID_STATUSES = frozenset({"pending", "running", "completed", "failed", "cancelled"})

MODEL_FILES = ("model.pt", "pytorch_model.bin", "model.safetensors")
MAX_DATASET_BYTES = 100 * 1024 * 1024  # 100 Mo
MAX_TEXTS_PER_CALL = 20
MAX_TEXT_CHARS = 2000


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    """Connexion SQLite en lecture stricte (mode=ro + PRAGMA query_only)."""
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    return conn


def _compact_job(payload: dict) -> dict:
    """Champs stables d'un TrainJob sérialisé (le reste est conservé dans job_get)."""
    keys = ("job_id", "status", "step", "started_at", "finished_at", "error", "model_path")
    return {key: payload.get(key) for key in keys}


# --- jobs ---------------------------------------------------------------------------
def job_list(status: str | None = None, limit: int = DEFAULT_JOB_LIMIT) -> dict:
    """Liste les jobs d'entraînement les plus récents (lecture seule).

    - `status` optionnel parmi pending/running/completed/failed/cancelled ;
    - tri par updated_at décroissant, `limit` plafonné à 100.
    """
    if status is not None and str(status) not in VALID_STATUSES:
        raise ValueError(
            f"Statut inconnu : '{status}'. Statuts valides : {sorted(VALID_STATUSES)}."
        )
    limit = max(1, min(int(limit), 100))
    db_path = safe_resolve(JOBS_DB_RELATIVE)
    if not db_path.is_file():
        return {
            "db_path": str(db_path),
            "job_count": 0,
            "truncated": False,
            "jobs": [],
            "message": "Aucune base de jobs (experiments/jobs.db absent) : aucun entraînement lancé.",
        }

    conn = _connect_readonly(db_path)
    try:
        rows = conn.execute(
            "SELECT job_id, payload, updated_at FROM jobs ORDER BY updated_at DESC"
        ).fetchall()
    finally:
        conn.close()

    filtered: list[dict] = []
    for job_id, payload_json, updated_at in rows:
        try:
            data = json.loads(payload_json)
        except json.JSONDecodeError:
            data = {"job_id": job_id,
                    "payload_corrupted": truncate_output(str(payload_json), 200)}
        entry = {"updated_at": iso_from_timestamp(updated_at), **_compact_job(data)}
        if status is None or str(entry.get("status")) == str(status):
            filtered.append(entry)

    return {
        "db_path": str(db_path),
        "job_count": min(len(filtered), limit),
        "truncated": len(filtered) > limit,
        "jobs": filtered[:limit],
    }


def job_get(job_id: str) -> dict:
    """Charge le payload COMPLET d'un job (hyperparamètres, erreur, chemin modèle)."""
    db_path = safe_resolve(JOBS_DB_RELATIVE)
    if not db_path.is_file():
        raise FileNotFoundError(f"Aucune base de jobs : {db_path}")
    conn = _connect_readonly(db_path)
    try:
        row = conn.execute(
            "SELECT payload, updated_at FROM jobs WHERE job_id = ?", (str(job_id),)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ValueError(
            f"Job introuvable : '{job_id}'. Utilisez job_list pour voir les jobs existants."
        )
    payload_json, updated_at = row
    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError:
        payload = {"payload_corrupted": truncate_output(str(payload_json), 500)}
    return {"updated_at": iso_from_timestamp(updated_at), **payload}


# --- prédiction -----------------------------------------------------------------------
def _get_predictor():
    """Import paresseux du cache de prédicteurs de l'API (testable par monkeypatch)."""
    try:
        from core.predictor_cache import get_predictor
    except ImportError as exc:
        raise RuntimeError(
            "core.predictor_cache inaccessible : lancez l'agent depuis la racine du projet."
        ) from exc
    try:
        return get_predictor()
    except Exception as exc:  # HTTPException 503 « Aucun modèle disponible », etc.
        raise RuntimeError(
            f"Prédicteur indisponible ({exc}) : entraînez d'abord un modèle "
            "(POST /train) ou vérifiez experiments/models/."
        ) from exc


def predict_sentiment(texts) -> dict:
    """Prédit le sentiment (positive/neutral/negative) d'une liste de textes FR/EN
    avec le modèle courant. 1 à 20 textes, 2000 caractères max chacun."""
    if isinstance(texts, str):
        texts = [texts]
    if not isinstance(texts, list) or not texts:
        raise ValueError("'texts' doit être une liste non vide de chaînes.")
    if len(texts) > MAX_TEXTS_PER_CALL:
        raise ValueError(f"Maximum {MAX_TEXTS_PER_CALL} textes par appel (reçu : {len(texts)}).")
    cleaned = []
    for index, text in enumerate(texts):
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"texts[{index}] doit être une chaîne non vide.")
        cleaned.append(text[:MAX_TEXT_CHARS])

    predictor = _get_predictor()
    predictions = predictor.predict(cleaned)
    return {"count": len(predictions), "predictions": predictions}


# --- datasets ---------------------------------------------------------------------------
_DATASET_SUFFIXES = {".csv", ".tsv", ".jsonl", ".json"}


def dataset_stats(path: str, sample_rows: int = 2000) -> dict:
    """Profil rapide d'un dataset CSV/TSV/JSONL sous la sandbox : lignes,
    colonnes, valeurs manquantes, distribution des labels/langues, doublons."""
    target = safe_resolve(path, must_exist=True)
    if not target.is_file():
        raise IsADirectoryError(f"Pas un fichier : {target}")
    if target.suffix.lower() not in _DATASET_SUFFIXES:
        raise ValueError(
            f"Format non supporté : '{target.suffix}'. Formats : "
            f"{sorted(_DATASET_SUFFIXES)} (CSV/TSV/JSONL)."
        )
    if target.stat().st_size > MAX_DATASET_BYTES:
        raise ValueError(
            f"Fichier trop volumineux ({target.stat().st_size} octets > {MAX_DATASET_BYTES})."
        )
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError("pandas indisponible : installez les requirements du projet.") from exc

    try:
        suffix = target.suffix.lower()
        if suffix == ".csv":
            frame = pd.read_csv(target)
        elif suffix == ".tsv":
            frame = pd.read_csv(target, sep="\t")
        elif suffix == ".jsonl":
            frame = pd.read_json(target, lines=True)
        else:  # .json tableau classique
            frame = pd.read_json(target)
    except Exception as exc:
        raise RuntimeError(f"Lecture du dataset impossible : {exc}") from exc

    sample_rows = max(10, min(int(sample_rows), len(frame) or 10))
    sample = frame.head(sample_rows)

    stats: dict = {
        "path": str(target),
        "format": target.suffix.lower(),
        "row_count": int(len(frame)),
        "columns": [str(col) for col in frame.columns],
        "sample_rows_used": int(len(sample)),
        "missing": {
            str(col): int(count)
            for col, count in sample.isna().sum().items()
            if int(count) > 0
        },
    }

    for column in ("label", "lang_code"):
        if column in sample.columns:
            # dropna : les valeurs manquantes sont déjà comptées dans « missing »,
            # les distributions ne décrivent que les valeurs présentes.
            counts = sample[column].dropna().astype(str).value_counts()
            stats[f"{column}_counts"] = {str(key): int(value) for key, value in counts.items()}
    if "text" in sample.columns:
        stats["duplicate_text_rows"] = int(sample["text"].duplicated().sum())
    return stats


# --- versions de modèles ------------------------------------------------------------------
def model_versions(model_root: str = "experiments/models") -> dict:
    """Liste les versions de modèles entraînés visibles dans la sandbox
    (mêmes conventions que core/model_versioning : nom de dossier contenant
    model.pt / pytorch_model.bin / model.safetensors ; tri décroissant,
    la première est la version 'active' par défaut de l'API)."""
    base = safe_resolve(model_root)
    if not base.is_dir():
        return {
            "model_root": str(base),
            "version_count": 0,
            "versions": [],
            "message": f"Aucun dossier de modèles : {base}. Lancez un entraînement (POST /train).",
        }

    versions: list[dict] = []
    for item in sorted(base.iterdir(), reverse=True):
        if not item.is_dir():
            continue
        if not any((item / filename).is_file() for filename in MODEL_FILES):
            continue
        versions.append(
            {
                "name": item.name,
                "path": item.relative_to(safe_resolve(".")).as_posix(),
                "created_at": iso_from_timestamp(item.stat().st_mtime),
                "active": not versions,  # la plus récente est active par défaut
            }
        )
    return {"model_root": str(base), "version_count": len(versions), "versions": versions}

# --- lancement d'entraînements ------------------------------------------------------------
# Champs acceptés par TrainRequest (core/models.py) : la validation pydantic de
# l'API est réutilisée telle quelle (y compris le validateur class_augment_weights).
TRAIN_REQUEST_FIELDS = (
    "max_per_lang",
    "local_corrections_path",
    "augment_fraction",
    "variants_per_example",
    "class_augment_weights",
    "epochs",
    "batch_size",
    "num_workers",
    "max_length",
    "learning_rate",
    "weight_decay",
    "warmup_ratio",
    "device",
)
_CORRECTIONS_SUFFIXES = {".csv", ".jsonl"}
_DEVICE_PREFIXES = ("auto", "cpu", "cuda")
TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled"})
TRAIN_POLL_INTERVAL_SECONDS = 0.5
TRAIN_DEFAULT_WAIT_TIMEOUT = 3600.0
MAX_TRAIN_WAIT_TIMEOUT = 24 * 3600.0


def _get_job_store():
    """Import paresseux du job store persistant (core.job_store)."""
    try:
        from core.job_store import get_job_store
    except ImportError as exc:
        raise RuntimeError(
            "core.job_store inaccessible : lancez l'agent depuis la racine du projet."
        ) from exc
    return get_job_store()


def _get_trainer_runner():
    """Import paresseux de core.trainer_runner (qui importe torch/transformers)."""
    try:
        from core.trainer_runner import run_training
    except ImportError as exc:
        raise RuntimeError(
            "core.trainer_runner inaccessible (torch/transformers manquants ?) : "
            "lancez l'agent depuis la racine du projet avec les requirements."
        ) from exc
    return run_training


def _build_train_request(params: dict):
    """Valide les hyperparamètres via le modèle pydantic TrainRequest de l'API
    (mêmes règles que POST /train) et retourne l'instance correspondante."""
    from core.models import TrainRequest  # pydantic uniquement, sans torch

    unknown = sorted(set(params) - set(TRAIN_REQUEST_FIELDS))
    if unknown:
        raise ValueError(
            f"Hyperparamètre(s) inconnu(s) : {unknown}. Champs acceptés (tous "
            f"optionnels) : {list(TRAIN_REQUEST_FIELDS)}."
        )
    device = params.get("device")
    if device is not None and not str(device).startswith(_DEVICE_PREFIXES):
        raise ValueError(
            f"Device invalide : '{device}'. Valeurs attendues : auto, cpu, cuda "
            "(ou cuda:N)."
        )
    corrections = params.get("local_corrections_path")
    if corrections is not None:
        resolved = safe_resolve(str(corrections), must_exist=True)
        if not resolved.is_file():
            raise IsADirectoryError(f"Pas un fichier de corrections : {resolved}")
        if resolved.suffix.lower() not in _CORRECTIONS_SUFFIXES:
            raise ValueError(
                f"Format de corrections non supporté : '{resolved.suffix}'. "
                f"Formats : {sorted(_CORRECTIONS_SUFFIXES)} (CSV/JSONL)."
            )
        params = {**params, "local_corrections_path": str(resolved)}
    try:
        return TrainRequest(**params)
    except ValueError as exc:  # pydantic ValidationError (hérite de ValueError)
        raise ValueError(f"Hyperparamètres d'entraînement invalides : {exc}") from exc


def _refuse_if_training_running(store) -> None:
    """Un seul entraînement à la fois : le runner écrit dans experiments/models."""
    for job in store.values():
        status = getattr(getattr(job, "status", None), "value", None)
        if status in ("pending", "running"):
            raise ValueError(
                f"Un entraînement est déjà en cours (job_id={getattr(job, 'job_id', '?')}, "
                f"statut={status}). Attendez sa fin (job_get) ou annulez-le avant "
                "d'en lancer un nouveau."
            )


def _training_thread_target(job_id: str, req) -> None:
    """Exécute run_training dans un thread daemon ; en cas d'échec d'import ou
    d'exécution, marque le job FAILED pour ne jamais le laisser en attente."""
    try:
        _get_trainer_runner()(job_id, req)
    except Exception as exc:  # le thread ne doit jamais laisser un job orphelin
        try:
            from core.models import JobStatus

            store = _get_job_store()
            job = store.get(job_id)
            if job is not None and job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                job.status = JobStatus.FAILED
                job.error = truncate_output(str(exc), 500)
                job.finished_at = time.time()
                store[job_id] = job
        except Exception:  # dernier recours : on n'élève jamais depuis un thread
            pass


def _launch_training_job(params: dict) -> dict:
    """Point commun start_training / train_model : validation des hyperparamètres,
    garde anti-concurrence, création du TrainJob PENDING puis démarrage du thread
    d'entraînement (même mécanique que POST /train dans api/routes/train.py)."""
    from core.models import JobStatus, TrainJob  # pydantic uniquement, sans torch

    req = _build_train_request(params)
    store = _get_job_store()
    _refuse_if_training_running(store)

    job_id = str(uuid.uuid4())
    job = TrainJob(job_id=job_id, status=JobStatus.PENDING)
    store[job_id] = job
    started_status = job.status.value  # snapshot AVANT le démarrage du thread
    threading.Thread(
        target=_training_thread_target, args=(job_id, req), daemon=True
    ).start()
    return {"job_id": job_id, "store": store, "started_at_status": started_status}


def start_training(**params) -> dict:
    """Lance un entraînement en ARRIÈRE-PLAN (même mécanique que POST /train) et
    retourne immédiatement le job_id ; suivez la progression avec job_get / job_list.

    Hyperparamètres optionnels : max_per_lang, local_corrections_path (CSV/JSONL
    de corrections sous la sandbox), augment_fraction, variants_per_example,
    class_augment_weights, epochs, batch_size, num_workers, max_length,
    learning_rate, weight_decay, warmup_ratio, device (auto/cpu/cuda)."""
    launched = _launch_training_job(params)
    return {
        "job_id": launched["job_id"],
        "status": launched["started_at_status"],
        "step": "queued",
        "message": (
            f"Entraînement lancé en arrière-plan (job_id={launched['job_id']}). "
            "Suivez la progression avec job_get(job_id=...) ou job_list(status='running')."
        ),
    }


def train_model(wait_timeout: float = TRAIN_DEFAULT_WAIT_TIMEOUT, **params) -> dict:
    """Lance un entraînement et ATTEND sa fin (bloquant, timeout en secondes).
    Retourne le statut final, le chemin du modèle sauvegardé et l'erreur éventuelle.
    Mêmes hyperparamètres optionnels que start_training."""
    wait_timeout = min(max(float(wait_timeout), 1.0), MAX_TRAIN_WAIT_TIMEOUT)
    launched = _launch_training_job(params)
    job_id, store = launched["job_id"], launched["store"]

    deadline = time.monotonic() + wait_timeout
    job = store.get(job_id)
    while job is not None and job.status.value not in TERMINAL_JOB_STATUSES:
        if time.monotonic() >= deadline:
            return {
                "job_id": job_id,
                "status": job.status.value,
                "step": job.step,
                "timed_out": True,
                "message": (
                    f"Entraînement toujours en cours après {wait_timeout:g}s "
                    f"(statut={job.status.value}). Continuez à suivre avec "
                    f"job_get(job_id='{job_id}') et ne relancez PAS d'entraînement "
                    "tant que celui-ci n'est pas terminé ou annulé."
                ),
            }
        time.sleep(TRAIN_POLL_INTERVAL_SECONDS)
        job = store.get(job_id)

    if job is None:  # pragma: no cover - le job vient d'être créé
        raise RuntimeError(f"Job {job_id} disparu du job store pendant l'attente.")

    if job.status.value == "completed":
        message = f"Entraînement terminé : modèle sauvegardé dans {job.model_path}."
    elif job.status.value == "failed":
        message = f"Entraînement échoué : {job.error}"
    else:
        message = f"Entraînement annulé (statut={job.status.value})."
    return {
        "job_id": job_id,
        "status": job.status.value,
        "step": job.step,
        "model_path": job.model_path,
        "error": job.error,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "timed_out": False,
        "message": message,
    }



def _get_training_canceller():
    """Import paresseux de core.trainer_runner.cancel_training (qui importe
    torch/transformers) : c'est le même cancel_event que lit la boucle du
    Trainer pour s'arrêter proprement au prochain point de contrôle."""
    try:
        from core.trainer_runner import cancel_training
    except ImportError as exc:
        raise RuntimeError(
            "core.trainer_runner inaccessible (torch/transformers manquants ?) : "
            "lancez l'agent depuis la racine du projet avec les requirements."
        ) from exc
    return cancel_training


def cancel_training(job_id: str) -> dict:
    """Demande l'ARRÊT d'un entraînement en cours (pending/running) via son job_id.

    Marque le job CANCELLED dans le job store et positionne le cancel_event que
    la boucle d'entraînement consulte : le thread s'arrête au prochain point de
    contrôle (pas de kill brutal). Confirmez avec job_get après quelques
    secondes. Seuls les jobs pending/running peuvent être annulés."""
    if not isinstance(job_id, str) or not job_id.strip():
        raise ValueError("'job_id' doit être une chaîne non vide (via job_list).")

    store = _get_job_store()
    job = store.get(str(job_id))
    if job is None:
        raise ValueError(
            f"Job introuvable : '{job_id}'. Utilisez job_list pour voir les "
            "jobs existants."
        )
    status = getattr(getattr(job, "status", None), "value", None)
    if status not in ("pending", "running"):
        raise ValueError(
            f"Job déjà terminé (statut={status}) : rien à annuler pour '{job_id}'."
        )

    cancelled = _get_training_canceller()(str(job_id))
    return {
        "job_id": cancelled.job_id,
        "status": cancelled.status.value,
        "step": cancelled.step,
        "message": (
            f"Arrêt demandé (job_id={cancelled.job_id}). Le thread s'arrête au "
            f"prochain point de contrôle : confirmez avec job_get(job_id='{cancelled.job_id}') "
            "avant de relancer un entraînement."
        ),
    }


def stop_training(job_id: str) -> dict:
    """Alias de cancel_training : demande l'arrêt propre d'un entraînement
    en cours (pending/running) via son job_id."""
    return cancel_training(job_id)
