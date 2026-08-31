import argparse
import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def normalize(label):
    if label is None:
        return "unknown"
    value = str(label).strip().lower()
    aliases = {
        "negatif": "negative",
        "négatif": "negative",
        "neutre": "neutral",
        "positif": "positive",
    }
    return aliases.get(value, value)


def load_review_csv(path: str):
    rows = []
    with open(path, "r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rows.append({
                "text": row.get("text", "").strip(),
                "predicted_label": normalize(row.get("predicted_label", "")),
                "manual_label": normalize(row.get("manual_label", "")),
                "status": (row.get("status", "") or "").strip().lower(),
            })
    return rows


def score_review(rows):
    valid = 0
    incorrect = 0
    for row in rows:
        if not row["text"]:
            continue
        if row["manual_label"] in {"negative", "neutral", "positive"}:
            valid += 1
            if row["predicted_label"] != row["manual_label"]:
                incorrect += 1

    total = valid
    accuracy = (valid - incorrect) / valid if valid else 0.0
    noise_rate = incorrect / valid if valid else 0.0
    return {
        "rows_evaluated": total,
        "correct": valid - incorrect,
        "incorrect": incorrect,
        "accuracy": accuracy,
        "noise_rate": noise_rate,
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    parser = argparse.ArgumentParser(description="Compute the quality score from a manually reviewed CSV.")
    parser.add_argument("--input", required=True, help="CSV created by manual review. It must contain text,predicted_label,manual_label,status.")
    args = parser.parse_args()

    data = load_review_csv(args.input)
    metrics = score_review(data)

    logger.info({
        "rows_evaluated": metrics["rows_evaluated"],
        "correct": metrics["correct"],
        "incorrect": metrics["incorrect"],
        "accuracy": round(metrics["accuracy"], 4),
        "noise_rate": round(metrics["noise_rate"], 4),
    })


if __name__ == "__main__":
    main()
