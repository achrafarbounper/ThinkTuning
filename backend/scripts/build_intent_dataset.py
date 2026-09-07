"""Auto-labellisation et fusion de datasets chat/action (Phase 4).

Deux modes :
  - auto-labellisation d'un corpus brut (``--input``, .txt/.csv) via les
    règles métier du fallback (marqueurs d'action) ;
  - fusion de datasets déjà étiquetés (``--merge``, fichiers .jsonl) avec
    déduplication par texte normalisé (casse/espaces).

Usage :
    python scripts/build_intent_dataset.py --input conversations.txt ^
        --output data/intent_dataset.jsonl [--min-action 1]
    python scripts/build_intent_dataset.py --merge a.jsonl --merge b.jsonl ^
        --output data/intent_dataset.jsonl

Le dataset produit alimente ensuite ``scripts/train_intent.py``.
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


def _load_labeled(path: Path) -> list[dict[str, str]]:
    """Charge un dataset JSONL déjà étiqueté (``{"text", "label"}``)."""
    records: list[dict[str, str]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            records.append({"text": str(row["text"]), "label": str(row["label"])})
    return records


def _normalize(text: str) -> str:
    """Clé de déduplication insensible à la casse et aux espaces superflus."""
    return " ".join(text.lower().split())


def merge_labeled(*datasets: list[dict[str, str]]) -> list[dict[str, str]]:
    """Fusionne des datasets étiquetés en préservant l'ordre (première
    occurrence gagne sur texte normalisé identique)."""
    merged: list[dict[str, str]] = []
    seen: set[str] = set()
    for records in datasets:
        for record in records:
            key = _normalize(record["text"])
            if key in seen:
                continue
            seen.add(key)
            merged.append(record)
    return merged


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-labellisation + fusion chat/action")
    parser.add_argument("--input", help="Fichier .txt ou .csv source (auto-labellisation)")
    parser.add_argument("--merge", action="append", default=[], metavar="JSONL",
                        help="Dataset JSONL déjà étiqueté à fusionner (répétable)")
    parser.add_argument("--output", default="data/intent_dataset.jsonl")
    parser.add_argument(
        "--min-action",
        type=int,
        default=1,
        help="Marqueurs d'action minimum pour labelliser 'action' (défaut 1)",
    )
    args = parser.parse_args()

    sources: list[list[dict[str, str]]] = []
    if args.merge:
        for path in args.merge:
            records = _load_labeled(Path(path))
            if not records:
                raise SystemExit(f"Aucune ligne valide dans {path}.")
            sources.append(records)
    if args.input:
        texts = _read_texts(Path(args.input))
        if not texts:
            raise SystemExit("Aucun texte lisible dans le fichier source.")
        sources.append(build_dataset(texts, min_action=args.min_action))
    if not sources:
        raise SystemExit("Rien à faire : fournissez --input ou --merge.")

    records = merge_labeled(*sources)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")

    counts: dict[str, int] = {"chat": 0, "action": 0}
    for record in records:
        label = record["label"]
        counts[label] = counts.get(label, 0) + 1
    print(
        f"{out} écrit : {len(records)} lignes "
        f"(chat={counts.get('chat', 0)}, action={counts.get('action', 0)})"
    )


if __name__ == "__main__":
    main()
