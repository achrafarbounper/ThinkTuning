# project/api/routes/drift.py
"""
Endpoint POST /drift — Détection de dérive entre deux batches de prédictions.

Compare la distribution des sentiments prédits sur deux batches (référence vs
production) pour détecter automatiquement une dérive.

Entrées acceptées :
- JSON  : {"texts_a": [...], "texts_b": [...]} (batch A = référence, B = production)
- multipart : deux fichiers CSV (file_a, file_b) + `text_column` (défaut "text")

Paramètres (query ou form) :
- `threshold` : seuil d'alerte (défaut 0.1). Pour KL : alerte si score > seuil.
  Pour chi² : alerte si p-value < seuil.
- `method`    : "kl" (KL divergence, défaut) ou "chi2" (test du khi-deux).
- `model`     : version du modèle (None => dernière version valide).

Sortie :
{
  "method": "kl",
  "drift_score": 0.42,          # KL divergence (nats) ou statistique chi²
  "p_value": null,              # uniquement pour chi2
  "threshold": 0.1,
  "drift_detected": true,
  "distribution_a": {...},      # proportions par label (batch référence)
  "distribution_b": {...},
  "n_a": 100, "n_b": 80
}
"""

import io
from collections import Counter

import numpy as np
import pandas as pd
from scipy.stats import chisquare, entropy
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

import api
from api.dependencies.auth import require_api_key

router = APIRouter(tags=["Drift"])

LABEL_NAMES = ["negative", "neutral", "positive"]

_DEFAULT_THRESHOLD = 0.1
_EPS = 1e-9  # lissage : évite division par zéro quand un label est absent d'un batch


def _validate_threshold(threshold: float) -> float:
    if threshold <= 0:
        raise HTTPException(status_code=400, detail="threshold doit être > 0")
    return threshold


def _read_csv_texts(raw: bytes, text_column: str) -> list[str]:
    """Lit un CSV uploadé et retourne la colonne de textes."""
    if not raw:
        raise HTTPException(status_code=400, detail="Empty file")
    try:
        df = pd.read_csv(io.BytesIO(raw))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid CSV")
    if text_column not in df.columns:
        raise HTTPException(status_code=400, detail="Missing text column")
    return [str(t) for t in df[text_column].tolist()]


def _predict_labels(predictor, texts: list[str]) -> list[str]:
    """Prédit les sentiments d'un batch et retourne la liste des labels."""
    results = predictor.predict(texts)
    labels = []
    for res in results:
        name = res.get("sentiment")
        if name not in LABEL_NAMES:
            raise HTTPException(
                status_code=500,
                detail=f"Label de prédiction inconnu : {name!r}",
            )
        labels.append(name)
    return labels


def _label_distribution(labels: list[str]) -> dict:
    """Proportions par label sur l'union fixe des classes (somme = 1.0)."""
    counts = Counter(labels)
    total = len(labels)
    return {name: counts.get(name, 0) / total for name in LABEL_NAMES}


def _counts_vector(labels: list[str]) -> np.ndarray:
    return np.array([Counter(labels).get(name, 0) for name in LABEL_NAMES], dtype=float)

def _drift_score(labels_a: list[str], labels_b: list[str], method: str) -> dict:
    """Calcule le score de dérive entre deux batches de labels.

    Retourne {"score": float, "p_value": float | None}.
    - "kl"   : divergence de Kullback-Leibler D(P_a || P_b) en nats, avec
               lissage epsilon sur les distributions (labels absents tolérés).
    - "chi2" : test du khi-deux sur les effectifs des deux batches (le score
               est la statistique chi², l'alerte se fait sur la p-value).
    """
    if method == "kl":
        dist_a = np.array([_label_distribution(labels_a)[n] for n in LABEL_NAMES]) + _EPS
        dist_b = np.array([_label_distribution(labels_b)[n] for n in LABEL_NAMES]) + _EPS
        score = float(entropy(dist_a, dist_b))
        return {"score": score, "p_value": None}

    if method == "chi2":
        counts_a = _counts_vector(labels_a)
        counts_b = _counts_vector(labels_b)
        # chisquare compare un échantillon à une fréquence attendue :
        # on teste B contre les proportions observées sur A (référence).
        # Les labels d'effectif attendu nul (absents du batch A) invalident le
        # test (division 0/0 -> NaN) : on les exclut du calcul.
        mask = counts_a > 0
        if int(mask.sum()) < 2:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Test du khi-deux non calculable : le batch de référence "
                    "ne couvre qu'un seul label. Utilisez method=kl ou fournissez "
                    "un batch de référence plus varié."
                ),
            )
        expected = counts_a[mask] / counts_a[mask].sum() * counts_b[mask].sum()
        stat, p_value = chisquare(counts_b[mask], f_exp=expected)
        # Sanitisation défensive : NaN/inf ne sont pas sérialisables en JSON.
        if not np.isfinite(stat) or not np.isfinite(p_value):
            return {"score": 0.0, "p_value": 1.0}
        return {"score": float(stat), "p_value": float(p_value)}

    raise HTTPException(status_code=400, detail="method doit être 'kl' ou 'chi2'")


@router.post("/drift")
async def drift_route(
    request: Request,
    file_a: UploadFile | None = File(default=None),
    file_b: UploadFile | None = File(default=None),
    text_column: str = Form(default="text"),
    _: bool = Depends(require_api_key),
):
    """Détecte une dérive de distribution des sentiments entre deux batches.

    Mode JSON  : body {"texts_a": [...], "texts_b": [...]}
    Mode CSV   : multipart avec file_a + file_b (CSV) et text_column.
    """
    # --- Paramètres communs (query d'abord, puis override form) --------------
    content_type = request.headers.get("content-type", "")
    form = None
    if "multipart/form-data" in content_type:
        form = await request.form()

    def _param(name: str, default):
        if form is not None and name in form and form[name] != "":
            return form[name]
        return request.query_params.get(name, default)

    threshold = _validate_threshold(float(_param("threshold", _DEFAULT_THRESHOLD)))
    method = str(_param("method", "kl")).lower()
    model = _param("model", None)

    # --- Extraction des deux batches -----------------------------------------
    if form is not None and (file_a is not None or file_b is not None):
        if file_a is None or file_b is None:
            raise HTTPException(
                status_code=400, detail="Deux fichiers CSV requis : file_a et file_b"
            )
        raw_a = await file_a.read()
        raw_b = await file_b.read()
        text_column = str(_param("text_column", "text"))
        texts_a = _read_csv_texts(raw_a, text_column)
        texts_b = _read_csv_texts(raw_b, text_column)
    else:
        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(
                status_code=400,
                detail="Body JSON invalide : attendu {\"texts_a\": [...], \"texts_b\": [...]} "
                       "ou deux fichiers CSV (file_a, file_b)",
            )
        if not isinstance(payload, dict) or "texts_a" not in payload or "texts_b" not in payload:
            raise HTTPException(
                status_code=400,
                detail="Champs requis : texts_a et texts_b (listes de textes)",
            )
        texts_a, texts_b = payload["texts_a"], payload["texts_b"]
        # Le body JSON peut aussi porter threshold / method / model (override query).
        if "threshold" in payload:
            threshold = _validate_threshold(float(payload["threshold"]))
        if "method" in payload:
            method = str(payload["method"]).lower()
        if not isinstance(texts_a, list) or not isinstance(texts_b, list):
            raise HTTPException(status_code=400, detail="texts_a et texts_b doivent être des listes")

    if not texts_a or not texts_b:
        raise HTTPException(status_code=400, detail="Les deux batches doivent être non vides")

    # --- Prédiction -----------------------------------------------------------
    predictor = api._get_predictor(model)
    labels_a = _predict_labels(predictor, [str(t) for t in texts_a])
    labels_b = _predict_labels(predictor, [str(t) for t in texts_b])

    # --- Score de dérive --------------------------------------------------------
    result = _drift_score(labels_a, labels_b, method)
    if method == "chi2":
        drift_detected = result["p_value"] < threshold
    else:
        drift_detected = result["score"] > threshold

    return {
        "method": method,
        "drift_score": round(result["score"], 6),
        "p_value": None if result["p_value"] is None else round(result["p_value"], 6),
        "threshold": threshold,
        "drift_detected": drift_detected,
        "distribution_a": _label_distribution(labels_a),
        "distribution_b": _label_distribution(labels_b),
        "n_a": len(labels_a),
        "n_b": len(labels_b),
    }
