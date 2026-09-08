"""Calibration du seuil de sécurité « action » (§13 checklist #5a).

Le seuil ``IntentClassifier.threshold`` (défaut ``0.5``) démotionne une
prédiction ``action`` dont la confiance est strictement inférieure au seuil :
le résultat devient ``chat`` (plus sûr de ne pas exécuter). Ce script mesure,
sur la fraction *val* d'un split stratifié du/des dataset(s) étiqueté(s),
l'accuracy et le F1 (macro + par classe) pour une grille de seuils — pour les
règles du fallback ET, si un modèle entraîné est disponible, pour le modèle
actif — puis recommande le seuil au meilleur F1 macro.

Pourquoi « baisser » le seuil est un no-op (cf. §13 #5a) :

  - moteur règles : ``fallback_intent`` renvoie ``action`` avec une confiance
    ``min(0.95, 0.62 + 0.08 * marqueurs)`` — toujours >= 0.62 ;
  - moteur modèle : softmax à 2 classes → la confiance de l'argmax est
    toujours >= 0.5.

Au défaut ``0.5``, la démotion ne peut donc jamais se déclencher : le seul
changement utile est de MONTER le seuil (sécuriser les actions peu sûres) —
la grille mesure précisément cet arbitrage rappel/précision.

Usage (depuis ``backend/``) :
    python scripts/calibrate_intent_threshold.py                  # règles + modèle
    python scripts/calibrate_intent_threshold.py --engine rules   # sans torch
    python scripts/calibrate_intent_threshold.py --step 0.01
    python scripts/calibrate_intent_threshold.py --model-version 20260907T232353Z
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# Console Windows (cp1252) : le rapport contient de l'Unicode (« ↔ », accents)
# — force l'UTF-8 avec repli tolérant plutôt qu'un UnicodeEncodeError.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from build_intent_dataset import merge_labeled  # noqa: E402
from core.intent_trainer import (  # noqa: E402
    _format_intent_report,
    _intent_classification_report,
    _split_records,
)
from ia.agent.classifiers.fallback import fallback_intent  # noqa: E402
from ia.agent.classifiers.intent_classifier import (  # noqa: E402
    IntentClassifier,
    apply_safety_threshold,
)

LABELS = ("chat", "action")  # ordre = indices de la tête (default_intent_labels)
_LABEL_IDS = {name: i for i, name in enumerate(LABELS)}
DEFAULT_DATASETS = ("data/intent_dataset.jsonl", "data/intent_dataset_extra.jsonl")


def _load_records(paths: list[Path]) -> list[list[dict[str, str]]]:
    """Charge chaque dataset JSONL (``{"text", "label"}``) séparément."""
    datasets: list[list[dict[str, str]]] = []
    for path in paths:
        rows: list[dict[str, str]] = []
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                rows.append({"text": str(row["text"]), "label": str(row["label"])})
        datasets.append(rows)
    return datasets


def _raw_rules(texts: list[str]) -> list[tuple[str, float]]:
    """Prédictions brutes des règles (aucune démotion appliquée)."""
    return [fallback_intent(text) for text in texts]


def _raw_model(
    texts: list[str], model_version: str | None
) -> tuple[list[tuple[str, float]], str]:
    """Prédictions brutes du modèle (``threshold=0`` → aucune démotion)."""
    classifier = IntentClassifier(
        model_name=model_version, engine="auto", threshold=0.0
    )
    classifier.load_model()
    engine = str(classifier.get_model_info()["engine"])
    results = classifier.predict(list(texts))
    return [(r.label, float(r.confidence)) for r in results], engine


def _sweep_thresholds(
    raw: list[tuple[str, float]], golds_ids: list[int], thresholds: list[float]
) -> list[dict]:
    """Métriques par seuil : démotion simulée via la règle partagée.

    ``golds_ids`` et les preds sont des **indices** de classe (0=chat, 1=action)
    — contrat de ``_intent_classification_report`` (target_names = LABELS).
    """
    rows: list[dict] = []
    for threshold in thresholds:
        preds = [
            _LABEL_IDS[apply_safety_threshold(label, confidence, threshold)[0]]
            for label, confidence in raw
        ]
        demoted = sum(
            1
            for (label, _), pred in zip(raw, preds)
            if label == "action" and pred == "chat"
        )
        diag = _intent_classification_report(preds, golds_ids, list(LABELS))
        accuracy = sum(1 for pred, gold in zip(preds, golds_ids) if pred == gold) / len(golds_ids)
        rows.append(
            {
                "threshold": threshold,
                "accuracy": accuracy,
                "f1_macro": diag["f1_macro"],
                "f1_action": diag["f1_per_class"].get("action", 0.0),
                "demoted": demoted,
            }
        )
    return rows


def _recommend(rows: list[dict], current: float) -> dict:
    """Meilleur F1 macro ; départage : accuracy, puis proximité du défaut."""
    return max(
        rows,
        key=lambda r: (r["f1_macro"], r["accuracy"], -abs(r["threshold"] - current)),
    )


def _print_table(rows: list[dict], current: float, best: dict) -> None:
    header = "threshold | accuracy | f1_macro | f1_action | démotés"
    print(header)
    print("-" * len(header))
    for row in rows:
        marks = ""
        if row["threshold"] == current:
            marks += "  * défaut actuel"
        if row is best:
            marks += "  <- recommandé"
        print(
            f"{row['threshold']:>9.2f} | {row['accuracy']:>8.3f} | "
            f"{row['f1_macro']:>8.3f} | {row['f1_action']:>9.3f} | "
            f"{row['demoted']:>7}{marks}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Calibration du seuil de sécurité « action » (§13 checklist #5a)"
    )
    parser.add_argument(
        "--engine",
        choices=("rules", "model", "both"),
        default="both",
        help="Moteurs à calibrer (défaut : both).",
    )
    parser.add_argument(
        "--dataset",
        action="append",
        default=None,
        help="Dataset JSONL (répétable). Défaut : les deux datasets du projet.",
    )
    parser.add_argument(
        "--test-size", type=float, default=0.1, help="Fraction val (0.1)."
    )
    parser.add_argument(
        "--seed", type=int, default=42, help="Graine du split stratifié (42)."
    )
    parser.add_argument("--step", type=float, default=0.05, help="Pas de la grille (0.05).")
    parser.add_argument(
        "--model-version", default=None, help="Version intent précise (défaut : active)."
    )
    parser.add_argument(
        "--current-default", type=float, default=0.5, help="Seuil actuel du code (0.5)."
    )
    args = parser.parse_args()

    paths = [Path(p) for p in (args.dataset or DEFAULT_DATASETS)]
    paths = [p if p.is_absolute() else _BACKEND / p for p in paths]
    datasets = _load_records(paths)
    records = merge_labeled(*datasets)
    counts = {label: sum(1 for r in records if r["label"] == label) for label in LABELS}
    print(f"Datasets : {', '.join(str(p.name) for p in paths)}")
    print(f"Total fusionné (dédupliqué) : {len(records)} exemples {counts}")

    train_records, val_records = _split_records(records, args.test_size, seed=args.seed)
    print(
        f"Split stratifié (seed={args.seed}) : train={len(train_records)} "
        f"val={len(val_records)}"
    )
    if not val_records:
        raise SystemExit("Val vide (dataset trop petit) : calibration impossible.")
    golds = [r["label"] for r in val_records]
    golds_ids = [_LABEL_IDS[g] for g in golds]
    texts = [r["text"] for r in val_records]

    thresholds = [
        round(i * args.step, 10) for i in range(int(round(1.0 / args.step)) + 1)
    ]

    engines: dict[str, list[tuple[str, float]]] = {}
    if args.engine in ("rules", "both"):
        engines["règles (fallback_intent)"] = _raw_rules(texts)
    if args.engine in ("model", "both"):
        try:
            raw_model, actual_engine = _raw_model(texts, args.model_version)
        except Exception as exc:  # pragma: no cover - environnement sans modèle
            print(f"Modèle indisponible ({exc}) : calibration règles seule.")
        else:
            if actual_engine == "rules":
                print("Aucun modèle actif : calibration règles seule.")
            else:
                engines[f"modèle ({actual_engine})"] = raw_model

    if not engines:
        raise SystemExit("Aucun moteur à calibrer.")

    for engine_name, raw in engines.items():
        print(f"\n=== {engine_name} — {len(raw)} exemples de val ===")
        rows = _sweep_thresholds(raw, golds_ids, thresholds)
        best = _recommend(rows, args.current_default)
        _print_table(rows, args.current_default, best)
        print(f"\nRapport au défaut {args.current_default} :")
        preds_default = [
            _LABEL_IDS[
                apply_safety_threshold(label, confidence, args.current_default)[0]
            ]
            for label, confidence in raw
        ]
        diag = _intent_classification_report(preds_default, golds_ids, list(LABELS))
        print(_format_intent_report(diag, list(LABELS)))
        print(
            f"\nRecommandation : threshold={best['threshold']} "
            f"(accuracy={best['accuracy']:.3f}, f1_macro={best['f1_macro']:.3f}, "
            f"démotions={best['demoted']})"
        )


if __name__ == "__main__":
    main()
