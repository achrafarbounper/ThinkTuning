# project/api/routes/evaluate.py
"""
Évaluation du modèle sur un échantillon de référence.

Expose un endpoint qui exécute le modèle (version choisie ou dernière version
valide) sur un échantillon du dataset et en déduit la matrice de confusion,
les erreurs par classe et les métriques globales. Ces données alimentent la
page « Évaluation » du dashboard (heatmap de confusion + comparaison v1 vs v2).
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from sklearn.metrics import confusion_matrix, f1_score

import api
from api.dependencies.auth import require_api_key

router = APIRouter(prefix="/evaluate", tags=["Évaluation"])

# Ordre des classes, aligné sur les labels 0, 1, 2 du dataset.
LABEL_NAMES = ["negative", "neutral", "positive"]
_LABEL_TO_ID = {name: idx for idx, name in enumerate(LABEL_NAMES)}


def _predict(predictor, texts):
    """Lance la prédiction sur une liste de textes.

    Renvoie une liste de dicts alignée sur `texts` :
        {"text": str, "pred_id": int, "pred_label": str, "confidence": float}

    `sentiment` étant un nom de classe ("negative"/"neutral"/"positive"), on le
    convertit en id 0/1/2 pour construire la matrice de confusion.
    """
    results = predictor.predict(texts)
    out = []
    for idx, res in enumerate(results):
        name = res.get("sentiment")
        label_id = _LABEL_TO_ID.get(name)
        if label_id is None:
            raise HTTPException(
                status_code=500,
                detail=f"Label de prédiction inconnu : {name!r}",
            )
        out.append({
            "text": str(texts[idx]),
            "pred_id": label_id,
            "pred_label": name,
            "confidence": float(res.get("confidence", 0.0)),
        })
    return out


def _confusion_pairs(matrix):
    """Paires de confusion hors-diagonale, triées par count décroissant.

    Chaque élément : {"true_label", "pred_label", "count"} avec
    true_label != pred_label.
    """
    pairs = []
    for true_idx, name in enumerate(LABEL_NAMES):
        for pred_idx, pred_name in enumerate(LABEL_NAMES):
            if true_idx == pred_idx:
                continue
            count = int(matrix[true_idx, pred_idx])
            if count > 0:
                pairs.append({
                    "true_label": name,
                    "pred_label": pred_name,
                    "count": count,
                })
    pairs.sort(key=lambda p: p["count"], reverse=True)
    return pairs


def _per_class_recall(matrix):
    """Rappel par classe (TP / (TP + FN)) depuis la matrice (lignes = vrai)."""
    row_sum = matrix.sum(axis=1)
    recalls = {}
    for idx, name in enumerate(LABEL_NAMES):
        if row_sum[idx] == 0:
            recalls[name] = None
        else:
            recalls[name] = round(float(matrix[idx, idx] / row_sum[idx]), 4)
    return recalls


@router.get("/confusion")
def confusion_route(
    model: str | None = None,
    limit: int = Query(default=300, ge=1, le=2000),
    max_mistakes: int = Query(default=100, ge=0, le=500),
    _: bool = Depends(require_api_key),
):
    """Matrice de confusion + erreurs par classe sur un échantillon de référence.

    - `model` : version du modèle à évaluer (None => dernière version valide).
    - `limit` : nombre d'exemples par langue chargés pour l'échantillon.
    - `max_mistakes` : nombre max d'exemples mal classés renvoyés (0 = aucun).
    """
    try:
        predictor = api._get_predictor(model)
    except HTTPException as exc:
        # Pas de modèle disponible -> même contrat que /predict (503).
        if exc.status_code == 503:
            raise
        raise

    raw = api.load_raw_dataset(max_per_lang=limit)

    texts = [str(t) for t in raw["text"]]
    labels = [int(l) for l in raw["label"]]

    if not texts:
        raise HTTPException(status_code=422, detail="Échantillon de référence vide.")

    predictions = _predict(predictor, texts)
    preds = [p["pred_id"] for p in predictions]

    matrix = confusion_matrix(labels, preds, labels=list(range(len(LABEL_NAMES))))

    total = int(matrix.sum())
    correct = int(matrix.trace())
    accuracy = round(correct / total, 4) if total else 0.0

    # F1 macro dérivé des métriques par classe (precision/recall).
    f1_macro = round(float(f1_score(labels, preds, average="macro", labels=list(range(len(LABEL_NAMES))))), 4)

    errors_by_class = []
    for idx, name in enumerate(LABEL_NAMES):
        row_total = sum(1 for lab in labels if lab == idx)
        tp = int(matrix[idx, idx])
        errors_by_class.append({
            "label": name,
            "total": row_total,
            "correct": tp,
            "errors": row_total - tp,
        })

    # Exemples mal classés (journal des erreurs), limités à max_mistakes.
    mistakes = []
    for true_label, p in zip(labels, predictions):
        if true_label != p["pred_id"]:
            mistakes.append({
                "text": p["text"],
                "true_label": LABEL_NAMES[true_label],
                "pred_label": p["pred_label"],
                "confidence": p["confidence"],
            })
            if len(mistakes) >= max_mistakes:
                break

    return {
        "model": model,
        "n": total,
        "labels": LABEL_NAMES,
        "matrix": matrix.tolist(),
        "metrics": {
            "accuracy": accuracy,
            "f1_macro": f1_macro,
            "per_class_recall": _per_class_recall(matrix),
        },
        "errors_by_class": errors_by_class,
        "mistakes": mistakes,
        "confusion_pairs": _confusion_pairs(matrix),
    }
