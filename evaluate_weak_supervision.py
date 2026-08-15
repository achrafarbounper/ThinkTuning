import argparse
import csv
import json
import random
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


LABELS = ["negative", "neutral", "positive"]


def normalize_label(value: Optional[str]) -> str:
    if value is None:
        return "unknown"
    label = str(value).strip().lower()
    aliases = {
        "negatif": "negative",
        "négatif": "negative",
        "neutre": "neutral",
        "positif": "positive",
        "bad": "negative",
        "good": "positive",
    }
    return aliases.get(label, label)


def load_jsonl(path: str) -> List[dict]:
    rows: List[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def load_csv(path: str, text_column: str = "text", label_column: str = "label") -> List[dict]:
    rows: List[dict] = []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if text_column not in row:
                continue
            rows.append({"text": row[text_column], "label": row.get(label_column, row.get("sentiment", ""))})
    return rows


def text_key(value: str) -> str:
    return " ".join(str(value).strip().lower().split())


def build_index(records: Iterable[dict], text_field: str = "text") -> Dict[str, dict]:
    index: Dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        raw_text = record.get(text_field, record.get("input", record.get("sentence", "")))
        if raw_text is None:
            continue
        index[text_key(raw_text)] = record
    return index


def sample_records(records: List[dict], sample_size: int, seed: int) -> List[dict]:
    if sample_size <= 0:
        return []
    if sample_size >= len(records):
        return records[:]
    rng = random.Random(seed)
    return rng.sample(records, sample_size)


def compare_labels(predicted_records: List[dict], gold_records: List[dict], sample_size: int, seed: int) -> dict:
    gold_index = build_index(gold_records, text_field="text")
    sampled = sample_records(predicted_records, sample_size, seed)

    mismatches: List[dict] = []
    total = 0
    ok = 0
    counts = Counter()

    for record in sampled:
        text = record.get("input", record.get("text", ""))
        pred_label = normalize_label(record.get("output", record.get("label", "")))
        gold_record = gold_index.get(text_key(text))
        if gold_record is None:
            if not text:
                continue
            # fallback search by input in gold_data without a text field
            gold_record = next(
                (r for r in gold_records if text_key(r.get("input", r.get("text", ""))) == text_key(text)),
                None,
            )
        if gold_record is None:
            mismatches.append({
                "text": text,
                "predicted": pred_label,
                "gold": "missing",
                "reason": "No manual match found",
            })
            counts["missing_gold"] += 1
            total += 1
            continue

        gold_label = normalize_label(gold_record.get("label", gold_record.get("sentiment", gold_record.get("output", ""))))
        total += 1
        counts["predicted"] += 1

        if pred_label == gold_label:
            ok += 1
        else:
            mismatches.append({
                "text": text,
                "predicted": pred_label,
                "gold": gold_label,
                "reason": "Label mismatch",
            })

    accuracy = ok / total if total else 0.0
    noise_rate = 1.0 - accuracy

    return {
        "sample_size": total,
        "matches": ok,
        "mismatches": total - ok,
        "accuracy": accuracy,
        "noise_rate": noise_rate,
        "mismatch_examples": mismatches[:10],
        "label_distribution": dict(counts),
    }


def write_manual_review_csv(records: List[dict], output_path: str):
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["text", "predicted_label", "manual_label", "status"])
        writer.writeheader()
        for record in records:
            writer.writerow({
                "text": record.get("text", ""),
                "predicted_label": record.get("predicted_label", record.get("label", record.get("output", ""))),
                "manual_label": "",
                "status": "",
            })


def write_jsonl(records: List[dict], output_path: str):
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def generate_sample_and_review(input_path: str, sample_size: int, seed: int, sample_out: Optional[str], review_out: Optional[str]):
    generated = load_jsonl(input_path)
    if not generated:
        raise ValueError(f"No records loaded from {input_path}")

    sampled = sample_records(generated, sample_size, seed)

    if sample_out:
        write_jsonl(sampled, sample_out)
        print(f"Sample written to {sample_out} ({len(sampled)} rows)")

    if review_out:
        review_records = []
        for record in sampled:
            text = record.get("text", record.get("input", ""))
            label = normalize_label(record.get("output", record.get("label", record.get("sentiment", ""))))
            review_records.append({
                "text": text,
                "predicted_label": label,
                "manual_label": "",
                "status": "",
            })
        write_manual_review_csv(review_records, review_out)
        print(f"Manual review CSV template written to {review_out}")

    return sampled


def main():
    parser = argparse.ArgumentParser(description="Estimate weak supervision noise by comparing a sample of generated labels to manual review.")
    parser.add_argument("--input", required=True, help="JSONL file with generated labels (e.g. label_dataset output).")
    parser.add_argument("--gold", default=None, help="CSV or JSONL file with manual labels (columns: text,label or sentiment). Optional if only generating a sample and a review template.")
    parser.add_argument("--sample_size", type=int, default=100, help="Number of records to sample for manual review.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for sampling.")
    parser.add_argument("--text_column", default="text", help="Column for text in structured manual review files.")
    parser.add_argument("--label_column", default="label", help="Column for manual label in CSV files.")
    parser.add_argument("--sample_out", default=None, help="Optional path for the sampled JSONL file to be manually reviewed.")
    parser.add_argument("--review_out", default=None, help="Optional path for the manual review CSV template.")
    args = parser.parse_args()

    generated = load_jsonl(args.input)
    if not generated:
        raise ValueError(f"No records loaded from {args.input}")

    sampled = generate_sample_and_review(
        args.input,
        sample_size=args.sample_size,
        seed=args.seed,
        sample_out=args.sample_out,
        review_out=args.review_out,
    )

    if args.gold:
        gold_path = Path(args.gold)
        if gold_path.suffix.lower() == ".csv":
            gold = load_csv(str(gold_path), text_column=args.text_column, label_column=args.label_column)
        else:
            gold = load_jsonl(str(gold_path))

        if not gold:
            raise ValueError(f"No manual labels loaded from {args.gold}")

        metrics = compare_labels(generated, gold, args.sample_size, args.seed)
        print(json.dumps({
            "sample_size": metrics["sample_size"],
            "matches": metrics["matches"],
            "mismatches": metrics["mismatches"],
            "accuracy": round(metrics["accuracy"], 4),
            "noise_rate": round(metrics["noise_rate"], 4),
            "examples": metrics["mismatch_examples"],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"Sample generation complete. {len(sampled)} records available for manual review.")


if __name__ == "__main__":
    main()
