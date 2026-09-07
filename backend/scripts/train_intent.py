"""Entraînement du classifieur d'intention chat/action (Phase 4).

Fine-tune un MiniLM (ou tout encodeur ``AutoModelForSequenceClassification``)
sur un dataset JSONL ``{"text", "label"}`` produit par
``scripts/build_intent_dataset.py``, puis sauvegarde une version dans
``experiments/intent_models/<horodatage>``.

Usage :
    python scripts/train_intent.py --dataset data/intent_dataset.jsonl \\
        --base "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2" \\
        --epochs 3 --quantize-int8

Note : le téléchargement du modèle de base et l'entraînement nécessitent
réseau + GPU/temps ; le pipeline d'inférence reste fonctionnel sans modèle
(repli règles), cet entraînement étant l'étape optionnelle de qualité.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.intent_store import (  # noqa: E402
    INTENT_MODEL_ROOT,
    default_intent_labels,
    list_intent_model_versions,
    set_active_intent_version,
)
# Parité API (cf. §13 de docs/INTENT_TRAINING.md) : helpers purs du runner
# intent, sans imports lourds — diagnostic classification_report (confusions
# chat↔action) + split train/val stratifié.
from core.intent_trainer import (  # noqa: E402
    _format_intent_report,
    _intent_classification_report,
    _split_records,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("train_intent")


def _load_records(dataset_path: Path) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    with open(dataset_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            records.append({"text": str(record["text"]), "label": str(record["label"])})
    return records


def _save_model(model, tokenizer, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(output_dir))
    tokenizer.save_pretrained(str(output_dir))
    logger.info("Version sauvegardée : %s", output_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description="Entraînement classifieur d'intention")
    parser.add_argument("--dataset", required=True, help="Dataset JSONL chat/action")
    parser.add_argument(
        "--base",
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        help="Modèle de base (identifiant HF)",
    )
    parser.add_argument("--epochs", type=int, default=3)
    parser.add_argument("--batch-size", type=int, default=32)
    parser.add_argument("--lr", type=float, default=2e-5)
    parser.add_argument("--max-length", type=int, default=128)
    parser.add_argument("--quantize-int8", action="store_true")
    parser.add_argument(
        "--activate",
        action="store_true",
        help="Pointe active.json sur cette version",
    )
    args = parser.parse_args()

    records = _load_records(Path(args.dataset))
    if not records:
        raise SystemExit("Dataset vide.")
    labels = list(default_intent_labels())
    counts = {label: sum(1 for r in records if r["label"] == label) for label in labels}
    logger.info("Dataset chargé : %d lignes (%s)", len(records), counts)

    try:
        import torch
        from datasets import Dataset
        from transformers import (
            AutoModelForSequenceClassification,
            AutoTokenizer,
            Trainer,
            TrainingArguments,
        )
    except ImportError as exc:
        raise SystemExit(
            "Dépendances d'entraînement absentes : torch / transformers / datasets."
        ) from exc

    def _to_label_id(label: str) -> int:
        if label not in labels:
            raise SystemExit(f"Label inconnu dans le dataset : {label!r} (attendu {labels})")
        return labels.index(label)

    # Split train/val STRATIFIÉ avant tokenisation (parité API — §13
    # checklist #2) : remplace l'historique Dataset.train_test_split(0.1, 42)
    # qui pouvait vider la val d'une classe par malchance.
    train_records, val_records = _split_records(records, 0.1)
    logger.info(
        "Split train/val stratifié : %d / %d", len(train_records), len(val_records)
    )

    tokenizer = AutoTokenizer.from_pretrained(args.base, use_fast=False)
    model = AutoModelForSequenceClassification.from_pretrained(
        args.base, num_labels=len(labels)
    )

    def _tokenize(batch):
        return tokenizer(
            batch["text"], padding="max_length", truncation=True,
            max_length=args.max_length,
        )

    def _build_dataset(recs: list) -> Dataset:
        return Dataset.from_list(
            [{"text": r["text"], "labels": _to_label_id(r["label"])} for r in recs]
        ).map(_tokenize, batched=True)

    train_ds = _build_dataset(train_records)
    # Val vide (dataset à 1 ligne) → évaluation désactivée (aucun crash).
    eval_ds = _build_dataset(val_records) if len(val_records) else None

    timestamp = datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = Path(INTENT_MODEL_ROOT) / timestamp

    training_args = TrainingArguments(
        output_dir=str(output_dir),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch_size,
        eval_strategy="epoch",
        logging_strategy="steps",
        logging_steps=20,
        learning_rate=args.lr,
        report_to=[],
    )

    def _compute_metrics(eval_pred) -> dict[str, float]:
        """Accuracy + finesse des probabilités + diagnostic classification.

        ``eval_avg_confidence`` proche de 0.5 signale un modèle qui hésite
        (entraînement insuffisant) ; ``eval_below_60pct`` est la part de
        prédictions rendues avec moins de 60 % de confiance. Le
        ``classification_report`` sklearn (§13) rend visibles les confusions
        ``chat ↔ action`` (rapport + matrice journalisés à chaque évaluation).
        """
        import numpy as np

        logits = np.asarray(getattr(eval_pred, "predictions", eval_pred[0]))
        labels_true = np.asarray(getattr(eval_pred, "label_ids", eval_pred[1]))
        exp = np.exp(logits - logits.max(axis=-1, keepdims=True))
        probs = exp / exp.sum(axis=-1, keepdims=True)
        preds = probs.argmax(axis=-1)
        top = probs[np.arange(preds.shape[0]), preds]
        diag = _intent_classification_report(
            preds, labels_true, list(default_intent_labels())
        )
        logger.info(
            "Classification report (chat↔action) :\n%s",
            _format_intent_report(diag, list(default_intent_labels())),
        )
        return {
            "eval_accuracy": float((preds == labels_true).mean()),
            "eval_avg_confidence": float(top.mean()),
            "eval_below_60pct": float((top < 0.6).mean()),
            "eval_f1_macro": diag["f1_macro"],
            "eval_f1_chat": diag["f1_per_class"].get("chat"),
            "eval_f1_action": diag["f1_per_class"].get("action"),
            "eval_confusion_matrix": diag["confusion_matrix"],
        }

    trainer = Trainer(
        model=model, args=training_args,
        train_dataset=train_ds, eval_dataset=eval_ds,
        compute_metrics=_compute_metrics,
    )
    trainer.train()
    eval_metrics = trainer.evaluate()
    logger.info(
        "Évaluation finale : accuracy=%.3f, f1_macro=%.3f, confiance moyenne=%.3f "
        "(%.1f%% des prédictions sous 60 %% de confiance)",
        eval_metrics.get("eval_accuracy", 0.0),
        eval_metrics.get("eval_f1_macro", 0.0),
        eval_metrics.get("eval_avg_confidence", 0.0),
        eval_metrics.get("eval_below_60pct", 0.0) * 100.0,
    )

    if args.quantize_int8:
        try:
            import torch.quantization as quant

            model = quant.quantize_dynamic(model, dtype=torch.qint8)
            logger.info("Quantification dynamique INT8 appliquée.")
        except Exception as exc:  # pragma: no cover - matériel/dépendances
            logger.warning("Quantisation INT8 indisponible (%s) ; modèle FP32 conservé.", exc)

    _save_model(model, tokenizer, output_dir)
    logger.info("Versions disponibles : %s", list_intent_model_versions())
    if args.activate:
        set_active_intent_version(timestamp)
        logger.info("Version active : %s", timestamp)


if __name__ == "__main__":
    main()

