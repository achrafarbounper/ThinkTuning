from sklearn.metrics import accuracy_score, f1_score


def compute_metrics(preds, labels):
    return {
        "accuracy": accuracy_score(labels, preds),
        "f1_macro": f1_score(labels, preds, average="macro"),
    }
