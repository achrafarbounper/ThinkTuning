"""Auto-labellisation d'un corpus brut en dataset chat/action (Phase 4).

Lit un fichier texte (utilisateurs, tickets, conversation… une phrase par
ligne) ou un CSV armé de la colonne texte fourni, et produit un dataset JSONL
``{"text": …, "label": "chat"|"action"}`` en appliquant les règles métier du
fallback (marqueurs d'action). Option ``--threshold`` : lignes sans marqueur
clair → ``chat``.

Usage :
    python scripts/build_intent_dataset.py --input conversations.txt \\
        --output data/intent_dataset.jsonl [--min-action 1]

Ce dataset alimente ensuite ``scripts/train_intent.py``.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ia.agent.classifiers.fallback import (
    fallback_intent,  # noqa: E402  # règle de repli (marqueurs)
)


def _read_texts(path: Path) -> list[str]:
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        if not rows:
            return []
        sample = rows[0]
        column = next(iter(sample))
        if "text" in sample:
            column = "text"
        return [r[column] for r in rows if r.get(column)]
    with open(path, encoding="utf-8") as fh:
        return [line.strip() for line in fh if line.strip()]


def build_dataset(texts: list[str], min_action: int = 1) -> list[dict[str, str]]:
    """Labellise chaque texte : ``action`` si >= ``min_action`` marqueur(s), sinon ``chat``."""
    records: list[dict[str, str]] = []
    for text in texts:
        label, _ = fallback_intent(text)
        # ``fallback_intent`` déclenche ``action`` dès 1 marqueur ; on permet
        # d'exiger un minimum de marqueurs pour désamorcer les faux positifs.
        if label == "action":
            from ia.agent.classifiers.fallback import _ACTION_MARKERS, _hits

            if _hits(text.lower(), _ACTION_MARKERS) < min_action:
                label = "chat"
        records.append({"text": text, "label": label})
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-labellisation chat/action")
    parser.add_argument("--input", required=True, help="Fichier .txt ou .csv source")
    parser.add_argument("--output", default="data/intent_dataset.jsonl")
    parser.add_argument(
        "--min-action",
        type=int,
        default=1,
        help="Marqueurs d'action minimum pour labelliser 'action' (défaut 1)",
    )
    args = parser.parse_args()

    texts = _read_texts(Path(args.input))
    if not texts:
        raise SystemExit("Aucun texte lisible dans le fichier source.")

    records = build_dataset(texts, min_action=args.min_action)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    counts = {"chat": 0, "action": 0}
    for record in records:
        counts[record["label"]] += 1
    print(
        f"{out} écrit : {len(records)} lignes "
        f"(chat={counts['chat']}, action={counts['action']})"
    )


if __name__ == "__main__":
    main()
