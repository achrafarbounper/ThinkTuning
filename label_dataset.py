import argparse
import csv
import json
import os
from pathlib import Path
from typing import Iterable, List, Optional

from core.model_versioning import resolve_model_path
from src.inference.predictor import Predictor

DEFAULT_INSTRUCTION = (
    "Classify the sentiment of the following text as negative, neutral, or positive."
)


def load_texts_from_file(file_path: str, text_column: str = "text") -> List[str]:
    """Charge une liste de textes depuis CSV, JSON, JSONL ou TXT.

    Les fichiers sont lus avec utf-8-sig : un éventuel BOM (exports
    Excel / PowerShell) est ignoré au lieu de corrompre la première colonne.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {file_path}")

    suffix = path.suffix.lower()

    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            texts = []
            for row in reader:
                value = row.get(text_column)
                if value is None:
                    continue
                texts.append(str(value).strip())
            return [text for text in texts if text]

    if suffix in {".json", ".jsonl"}:
        if suffix == ".jsonl":
            records = []
            with path.open("r", encoding="utf-8-sig") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    records.append(json.loads(line))
        else:
            with path.open("r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
            if isinstance(payload, list):
                records = payload
            elif isinstance(payload, dict):
                if "records" in payload and isinstance(payload["records"], list):
                    records = payload["records"]
                elif "data" in payload and isinstance(payload["data"], list):
                    records = payload["data"]
                else:
                    records = [payload]
            else:
                raise ValueError(f"Unsupported JSON payload type in {file_path}: {type(payload).__name__}")

        texts = []
        for row in records:
            if isinstance(row, dict):
                value = row.get(text_column)
                if value is not None:
                    texts.append(str(value).strip())
            elif isinstance(row, str):
                texts.append(row.strip())
        return [text for text in texts if text]

    if suffix in {".txt", ".md"}:
        with path.open("r", encoding="utf-8-sig") as handle:
            return [line.strip() for line in handle if line.strip()]

    raise ValueError(f"Unsupported input format for {file_path}: {suffix or 'unknown'}")


def build_alpaca_record(text: str, sentiment: str, confidence: float) -> dict:
    return {
        "instruction": DEFAULT_INSTRUCTION,
        "input": text,
        "output": sentiment,
        "confidence": float(confidence),
    }


def _chunked(iterable: Iterable[str], size: int):
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def label_dataset(
    input_path: str,
    output_path: str,
    model_path: Optional[str] = None,
    text_column: str = "text",
    min_confidence: float = 0.7,
    batch_size: int = 32,
):
    """Prédit le sentiment d'un fichier de texte et exporte le résultat en JSONL Alpaca.

    model_path : None -> dernière version valide dans experiments/models ;
                 nom de version (ex: 20260819T151459Z) ou chemin de dossier.
    """
    texts = load_texts_from_file(input_path, text_column=text_column)
    if not texts:
        return []

    predictor = Predictor(resolve_model_path(model_path))
    records = []

    for batch in _chunked(texts, batch_size):
        predictions = predictor.predict(batch)
        for pred in predictions:
            confidence = float(pred.get("confidence", 0.0))
            if confidence < min_confidence:
                continue
            records.append(build_alpaca_record(pred["text"], pred["sentiment"], confidence))

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    return records


def main():
    parser = argparse.ArgumentParser(description="Label a dataset with DistilBERT sentiment predictions and export Alpaca JSONL.")
    parser.add_argument("--input", required=True, help="CSV/JSON/JSONL/TXT input file containing texts.")
    parser.add_argument("--output", required=True, help="Target JSONL file for Alpaca records.")
    parser.add_argument(
        "--model_path",
        default=None,
        help="Version dans experiments/models (ex: 20260819T151459Z) or path to a model directory. Default: latest version.",
    )
    parser.add_argument("--text_column", default="text", help="Column name containing the text in structured files.")
    parser.add_argument(
        "--min_confidence",
        "--threshold",
        dest="min_confidence",
        type=float,
        default=0.7,
        help="Minimum confidence threshold to keep predictions.",
    )
    parser.add_argument("--batch_size", type=int, default=32, help="Batch size used for DistilBERT inference.")
    args = parser.parse_args()

    model_dir = resolve_model_path(args.model_path)
    print(f"Using model: {model_dir}")

    records = label_dataset(
        input_path=args.input,
        output_path=args.output,
        model_path=model_dir,
        text_column=args.text_column,
        min_confidence=args.min_confidence,
        batch_size=args.batch_size,
    )

    print(f"Exported {len(records)} records to {args.output}")


if __name__ == "__main__":
    main()
