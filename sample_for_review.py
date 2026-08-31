import argparse
import csv
import json
import logging
import random
from pathlib import Path

logger = logging.getLogger(__name__)


def load_jsonl(path: str):
    rows = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def sample_records(records, sample_size: int, seed: int):
    if sample_size <= 0:
        return []
    if sample_size >= len(records):
        return records[:]
    rng = random.Random(seed)
    return rng.sample(records, sample_size)


def write_sample_csv(rows, output_path: str):
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["text", "predicted_label", "manual_label", "status"])
        writer.writeheader()
        for row in rows:
            text = row.get("text") or row.get("input") or ""
            predicted = row.get("output") or row.get("label") or row.get("sentiment") or ""
            writer.writerow({
                "text": text,
                "predicted_label": predicted,
                "manual_label": "",
                "status": "",
            })


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    parser = argparse.ArgumentParser(description="Sample a random subset of generated labels for manual review.")
    parser.add_argument("--input", required=True, help="JSONL file with generated labels.")
    parser.add_argument("--sample_size", type=int, default=100, help="Size of the random sample.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility.")
    parser.add_argument("--output", default="data/sample_review.csv", help="Output CSV for manual review.")
    args = parser.parse_args()

    rows = load_jsonl(args.input)
    if not rows:
        raise ValueError(f"No records found in {args.input}")

    sampled = sample_records(rows, args.sample_size, args.seed)
    write_sample_csv(sampled, args.output)
    logger.info(f"Sample created: {len(sampled)} rows -> {args.output}")


if __name__ == "__main__":
    main()
