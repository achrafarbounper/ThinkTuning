from sklearn.metrics import accuracy_score, f1_score

# Ordre canonique des classes, aligné sur src/dataset/loader.py::LABEL_NAMES.
LABEL_ORDER = ["negative", "neutral", "positive"]


def compute_metrics(preds, labels):
    """Métriquesde de classification globales + F1 par classe.

    Rétro-compatible : ``accuracy`` et ``f1_macro`` gardent exactement les mêmes
    valeurs. On enrichit le dict du ``f1_per_class`` (utile au rapport d'entraînement
    exigé par le pipeline de ré-entraînement du cycle Active Learning)..
    """
    metrics = {
        "accuracy": accuracy_score(labels, preds),
        "f1_macro": f1_score(labels, preds, average="macro"),
    }

    try:
        per_class = f1_score(labels, preds, average=None, labels=list(range(len(LABEL_ORDER))), zero_division=0)
        metrics["f1_per_class"] = {
            name: float(per_class[idx])
            for idx, name in enumerate(LABEL_ORDER)
        }
    except Exception:
        # Un dataset sans toutes les classes présente ne doit jamais casser l'entraînement.
        metrics["f1_per_class"] = {name: None for name in LABEL_ORDER}

    return metrics
