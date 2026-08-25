"""Outils métier ThinkTuning pour l'agent IA : jobs d'entraînement, prédiction
de sentiment, profil de dataset et versions de modèles.

Donne à l'agent une vue STRUCTURÉE de la plateforme sans lui faire écrire du
SQL brut ni importer torch inutilement :
    - job_list / job_get : experiments/jobs.db en lecture seule stricte ;
    - predict_sentiment  : prédicteur courant via le cache core/predictor_cache
      (import paresseux : tests et environnements sans modèle fonctionnent) ;
    - dataset_stats      : profil CSV/TSV/JSONL via pandas ;
    - model_versions     : scan sandbox de experiments/models (mêmes conventions
      que core/model_versioning.py).

Sécurité : SQLite mode ro + PRAGMA query_only, chemins confinés par
safe_resolve, listes bornées, aucune variable d'environnement exposée.
"""

import json
import sqlite3
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