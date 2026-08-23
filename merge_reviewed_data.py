"""
Réinjection des corrections de review manuelle dans le jeu d'entraînement.

Ferme la boucle d'active learning :

    active_learning.py  ->  review manuelle (manual_review_template.csv)
                        ->  merge_reviewed_data.py  ->  train.py

Le script charge un CSV de review au format
``text,predicted_label,manual_label,status`` (généré par active_learning.py,
complété à la main), ne conserve que les lignes où ``manual_label`` est
renseigné avec une valeur valide (negative / neutral / positive — les alias
français négatif/neutre/positif sont acceptés, comme dans
score_manual_review.py), convertit ce label en entier cohérent avec
``LABEL_NAMES`` de ``src/dataset/loader.py``
({0: negative, 1: neutral, 2: positive}), puis fusionne ces exemples corrigés
avec le jeu d'entraînement source :

- la déduplication se fait sur le texte normalisé (trim + minuscules, même
  approche que ``augment_dataset``) : chaque texte n'apparaît qu'une fois ;
- si le texte existe déjà dans la source, sa ligne est MISE À JOUR avec le
  label corrigé (la correction manuelle prime, sinon la boucle d'amélioration
  n'aurait aucun effet sur les exemples déjà connus du modèle) ;
- sinon l'exemple est ajouté avec ``lang_code`` = valeur de ``--lang_code``.

Usage :
    python merge_reviewed_data.py --review data/manual_review_template.csv
        # fusionne avec le dataset HF cardiffnlp/tweet_sentiment_multilingual
        # (même source que train.py) et écrit data/train_enriched.jsonl
    python merge_reviewed_data.py --review data/manual_review_template.csv --source data/train.jsonl --output data/train_enriched.jsonl
    python merge_reviewed_data.py --review data/review.csv --source data/train.csv --output data/train_enriched.csv --format csv --lang_code en
"""

import argparse
import csv
import hashlib
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.dataset.loader import LABEL_NAMES, load_local_dataset, load_raw_dataset

# Format attendu pour le CSV de review (généré par active_learning.py /
# sample_for_review.py, complété manuellement).
REVIEW_FIELDS = ["text", "predicted_label", "manual_label", "status"]

VALID_MANUAL_LABELS = {"negative", "neutral", "positive"}

# Alias français acceptés dans manual_label / predicted_label (aligné sur
# score_manual_review.normalize).
LABEL_ALIASES = {
    "negatif": "negative",
    "négatif": "negative",
    "neutre": "neutral",
    "positif": "positive",
}

# Mapping label textuel -> entier, cohérent avec LABEL_NAMES de
# src/dataset/loader.py : {0: negative, 1: neutral, 2: positive}.
LABEL_TO_INT = {name: index for index, name in LABEL_NAMES.items()}

# Valeur spéciale de --source : dataset Hugging Face chargé via
# load_raw_dataset, exactement comme dans train.py.
HF_SOURCE = "hf"

DEFAULT_REVIEW = "data/manual_review_template.csv"
DEFAULT_SOURCE = HF_SOURCE
DEFAULT_OUTPUT = "data/train_enriched.jsonl"


def normalize_text(text) -> str:
    """Normalisation partagée avec la déduplication d'augment_dataset."""
    return str(text or "").strip().lower()


def text_key(text) -> str:
    """Empreinte stable d'un texte normalisé (même approche que loader.py)."""
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def normalize_manual_label(raw) -> str:
    """Normalise un label saisi à la main (trim, minuscules, alias français)."""
    value = "" if raw is None else str(raw).strip().lower()
    return LABEL_ALIASES.get(value, value)


def load_review_csv(review_path: str) -> List[Dict[str, str]]:
    """
    Charge le CSV de review complété (utf-8-sig : tolère le BOM des exports
    Excel / PowerShell, comme label_dataset.load_texts_from_file).
    """
    path = Path(review_path)
    if not path.exists():
        raise FileNotFoundError(f"Review file not found: {review_path}")

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = []
        for row in reader:
            rows.append({
                "text": (row.get("text") or "").strip(),
                "predicted_label": normalize_manual_label(row.get("predicted_label")),
                "manual_label": normalize_manual_label(row.get("manual_label")),
                "status": (row.get("status") or "").strip(),
            })
    return rows


def extract_valid_corrections(rows: List[Dict[str, str]]) -> List[Dict]:
    """
    Ne conserve que les lignes réellement tranchées : ``manual_label`` renseigné
    avec negative / neutral / positive (alias français acceptés). Les lignes
    vides ou non tranchées sont ignorées, et un même texte normalisé n'est
    compté qu'une seule fois (première occurrence gagnante).

    Returns:
        Liste de dicts {"text": str, "label": int}.
    """
    corrections: List[Dict] = []
    seen_keys = set()
    for row in rows:
        label_name = normalize_manual_label(row.get("manual_label"))
        if label_name not in VALID_MANUAL_LABELS:
            continue  # ligne vide ou non tranchée -> ignorée
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        key = text_key(text)
        if key in seen_keys:
            continue  # doublon interne au CSV de review
        seen_keys.add(key)
        corrections.append({
            "text": text,
            "label": LABEL_TO_INT[label_name],
        })
    return corrections


def merge_corrections_into_source(
    corrections: List[Dict],
    source_records: List[Dict],
    default_lang_code: str = "fr",
) -> Tuple[List[Dict], Dict]:
    """
    Fusionne les exemples corrigés avec le jeu d'entraînement source.

    - Un texte déjà présent (comparaison du texte normalisé, cf. text_key)
      voit son label mis à jour avec la correction manuelle : la correction
      prime, sans créer de doublon.
    - Un texte nouveau est ajouté avec lang_code = default_lang_code.

    Returns:
        (records fusionnés, statistiques) — chaque record est un dict
        {"text", "label", "lang_code"}.
    """
    merged: List[Dict] = []
    index: Dict[str, int] = {}
    source_duplicates_removed = 0

    for record in source_records:
        text = str(record.get("text") or "").strip()
        if not text:
            continue
        key = text_key(text)
        if key in index:
            source_duplicates_removed += 1
            continue  # doublon interne à la source
        index[key] = len(merged)
        merged.append({
            "text": text,
            "label": int(record["label"]),
            "lang_code": str(record.get("lang_code") or default_lang_code).strip() or default_lang_code,
        })

    corrected_existing = 0
    appended_new = 0
    duplicate_corrections_skipped = 0
    corrected_keys = set()
    for correction in corrections:
        key = text_key(correction["text"])
        position = index.get(key)
        if position is None:
            index[key] = len(merged)
            merged.append({
                "text": correction["text"],
                "label": correction["label"],
                "lang_code": default_lang_code,
            })
            appended_new += 1
        elif key in corrected_keys:
            duplicate_corrections_skipped += 1
            continue  # doublon normalisé au sein des corrections -> ignoré
        else:
            merged[position]["label"] = correction["label"]  # la correction prime
            corrected_keys.add(key)
            corrected_existing += 1

    stats = {
        "source_rows": len(source_records),
        "source_duplicates_removed": source_duplicates_removed,
        "corrected_existing": corrected_existing,
        "appended_new": appended_new,
        "duplicate_corrections_skipped": duplicate_corrections_skipped,
        "final_rows": len(merged),
    }
    return merged, stats


def write_merged_jsonl(records: List[Dict], output_path: str):
    """Exporte les records au format JSONL (une ligne JSON par exemple)."""
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps({
                "text": record["text"],
                "label": int(record["label"]),
                "lang_code": record["lang_code"],
            }, ensure_ascii=False) + "\n")


def write_merged_csv(records: List[Dict], output_path: str):
    """Exporte les records au format CSV (text,label,lang_code)."""
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["text", "label", "lang_code"])
        writer.writeheader()
        for record in records:
            writer.writerow({
                "text": record["text"],
                "label": int(record["label"]),
                "lang_code": record["lang_code"],
            })


def load_source_records(
    source: str,
    languages: List[str],
    max_per_lang: Optional[int],
) -> List[Dict]:
    """
    Charge le jeu d'entraînement source : soit le dataset Hugging Face
    (source == "hf", même source que train.py), soit un fichier local
    CSV/JSON/JSONL via load_local_dataset.
    """
    if source == HF_SOURCE:
        dataset = load_raw_dataset(languages=tuple(languages), max_per_lang=max_per_lang)
    else:
        dataset = load_local_dataset(source)

    return [
        {
            "text": record["text"],
            "label": record["label"],
            "lang_code": record.get("lang_code") or "",
        }
        for record in dataset
    ]


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Réinjecte les corrections de manual_review_template.csv dans le jeu "
            "d'entraînement (boucle d'active learning), avec déduplication sur "
            "le texte normalisé."
        )
    )
    parser.add_argument(
        "--review",
        default=DEFAULT_REVIEW,
        help=f"CSV de review complété, format text,predicted_label,manual_label,status (défaut: {DEFAULT_REVIEW}).",
    )
    parser.add_argument(
        "--source",
        default=DEFAULT_SOURCE,
        help=(
            f"'{HF_SOURCE}' (défaut) pour le dataset Hugging Face utilisé par train.py, "
            "ou chemin d'un fichier CSV/JSON/JSONL local (colonnes text/label/lang_code, "
            "alias input/output/sentiment acceptés)."
        ),
    )
    parser.add_argument(
        "--languages",
        default="fr,en",
        help="Langues chargées quand --source=hf (défaut: fr,en).",
    )
    parser.add_argument(
        "--max_per_lang",
        type=int,
        default=None,
        help="Limite d'exemples par langue quand --source=hf (défaut: tout le dataset).",
    )
    parser.add_argument(
        "--lang_code",
        choices=["fr", "en"],
        default="fr",
        help="Langue affectée aux exemples ajoutés qui ne proviennent pas de la source (défaut: fr).",
    )
    parser.add_argument(
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Fichier enrichi en sortie (défaut: {DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--format",
        choices=["jsonl", "csv"],
        dest="format",
        default="jsonl",
        help="Format d'export, réutilisable par train.py --dataset_file (défaut: jsonl).",
    )
    args = parser.parse_args()

    rows = load_review_csv(args.review)
    corrections = extract_valid_corrections(rows)
    print(f"Lignes de review : {len(rows)} | corrections valides : {len(corrections)}")
    if not corrections:
        print(
            "Aucune ligne tranchée (colonne manual_label vide ou invalide) : "
            "rien à fusionner, seule la source dédupliquée sera exportée."
        )

    languages = [lang.strip() for lang in args.languages.split(",") if lang.strip()]
    source_records = load_source_records(args.source, languages, args.max_per_lang)

    merged, stats = merge_corrections_into_source(
        corrections,
        source_records,
        default_lang_code=args.lang_code,
    )

    if args.format == "csv":
        write_merged_csv(merged, args.output)
    else:
        write_merged_jsonl(merged, args.output)

    print(
        f"Fusion terminée : {stats['source_rows']} exemples sources "
        f"({stats['source_duplicates_removed']} doublons retirés), "
        f"{stats['corrected_existing']} labels corrigés, "
        f"{stats['appended_new']} exemples ajoutés."
    )
    print(f"Exported {stats['final_rows']} examples to {args.output}")


if __name__ == "__main__":
    main()
