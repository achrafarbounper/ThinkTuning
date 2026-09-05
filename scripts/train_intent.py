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

    dataset = Dataset.from_list(
        [{"text": r["text"], "labels": _to_label_id(r["label"])} for r in records]
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

    dataset = dataset.map(_tokenize, batched=True)
    split = dataset.train_test_split(test_size=0.1, seed=42)
    train_ds, eval_ds = split["train"], split["test"]

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

    trainer = Trainer(
        model=model, args=training_args,
        train_dataset=train_ds, eval_dataset=eval_ds,
    )
    trainer.train()
    logger.info("Entraînement terminé. Métriques : %s", trainer.evaluate())

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

