"""
Chargement d'un dataset multilingue (français + anglais) pour la classification
de sentiments, avec possibilité d'appliquer une augmentation EDA.

Dataset : cardiffnlp/tweet_sentiment_multilingual
Chargé via les fichiers Parquet auto-convertis par Hugging Face.
"""

import csv
import hashlib
import json
import logging
import random
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd
from datasets import load_dataset, Dataset, concatenate_datasets

logger = logging.getLogger(__name__)

LABEL_NAMES = {0: "negative", 1: "neutral", 2: "positive"}

# Pondération par défaut de la sélection des exemples à augmenter. La classe
# neutral (label 1) est surpondérée car typiquement sous-représentée dans le
# corpus : des exemples neutral sont donc préférentiellement sur-échantillonnés.
DEFAULT_CLASS_AUGMENT_WEIGHTS: Dict[int, float] = {0: 1.0, 1: 2.0, 2: 1.0}

_PARQUET_BASE = (
    "https://huggingface.co/datasets/cardiffnlp/tweet_sentiment_multilingual"
    "/resolve/refs%2Fconvert%2Fparquet"
)

_LANG_CONFIG = {"fr": "french", "en": "english"}


def load_raw_dataset(
    languages: Iterable[str] = ("fr", "en"),
    max_per_lang: Optional[int] = 3000,
    seed: int = 42,
) -> Dataset:
    """
    Charge les sous-ensembles de langues demandées depuis les fichiers Parquet HF.

    Args:
        languages: liste des langues à charger ("fr", "en")
        max_per_lang: limite d'exemples par langue
        seed: graine aléatoire

    Returns:
        Dataset Hugging Face concaténé
    """
    subsets = []

    for lang_code in languages:
        config = _LANG_CONFIG[lang_code]
        data_files = {"train": f"{_PARQUET_BASE}/{config}/train/0000.parquet"}

        ds = load_dataset("parquet", data_files=data_files, split="train")

        if max_per_lang:
            ds = ds.shuffle(seed=seed).select(range(min(max_per_lang, len(ds))))

        ds = ds.add_column("lang_code", [lang_code] * len(ds))
        subsets.append(ds)

    return concatenate_datasets(subsets)


_TEXT_COLUMN_ALIASES = ("text", "input")
_LABEL_COLUMN_ALIASES = ("label", "output", "sentiment")


def coerce_label(value) -> int:
    """
    Convertit une valeur de label en entier valide selon LABEL_NAMES.

    Accepte les entiers (0, 1, 2), leurs représentations textuelles ("0",
    "2.0") ainsi que les noms de classes ("negative" / "neutral" / "positive").
    """
    name_to_int = {name: index for index, name in LABEL_NAMES.items()}

    if isinstance(value, bool):
        raise ValueError(f"Invalid label value: {value!r}")
    if isinstance(value, (int, np.integer)):
        label = int(value)
    else:
        text = "" if value is None else str(value).strip().lower()
        if not text:
            raise ValueError("Missing label value")
        if text in name_to_int:
            return name_to_int[text]
        try:
            label = int(float(text))
        except ValueError:
            raise ValueError(f"Unknown label value: {value!r}") from None

    if label in LABEL_NAMES:
        return label
    raise ValueError(f"Label out of range for {LABEL_NAMES}: {value!r}")


def _read_records_from_file(path: Path) -> List[dict]:
    """Lit un fichier CSV, JSON ou JSONL et renvoie une liste de dictionnaires."""
    suffix = path.suffix.lower()

    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]

    if suffix in {".json", ".jsonl"}:
        if suffix == ".jsonl":
            records = []
            with path.open("r", encoding="utf-8-sig") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        else:
            with path.open("r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
            if isinstance(payload, list):
                records = payload
            elif isinstance(payload, dict):
                if isinstance(payload.get("records"), list):
                    records = payload["records"]
                elif isinstance(payload.get("data"), list):
                    records = payload["data"]
                else:
                    records = [payload]
            else:
                raise ValueError(
                    f"Unsupported JSON payload type in {path}: {type(payload).__name__}"
                )
        return [record for record in records if isinstance(record, dict)]

    raise ValueError(f"Unsupported input format for {path}: {suffix or 'unknown'}")


def load_local_dataset(
    file_path: str,
    default_lang_code: str = "fr",
) -> Dataset:
    """
    Charge un jeu de données local (CSV, JSON ou JSONL) en Dataset HF avec les
    colonnes 'text', 'label' (entier) et 'lang_code'.

    Colonnes reconnues : 'text' ou 'input' pour le texte ; 'label', 'output'
    ou 'sentiment' pour le label (entier ou nom de classe, cf. coerce_label) ;
    'lang_code' optionnel (sinon default_lang_code).

    Utilisée notamment par train.py --dataset_file pour consommer le dataset
    enrichi produit par merge_reviewed_data.py.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {file_path}")

    texts: List[str] = []
    labels: List[int] = []
    langs: List[str] = []

    for row in _read_records_from_file(path):
        text_value = next((row[column] for column in _TEXT_COLUMN_ALIASES if row.get(column)), None)
        if text_value is None:
            continue
        text = str(text_value).strip()
        if not text:
            continue

        # Attention : 0 est un label valide, on teste donc explicitement None.
        label_value = next(
            (row[column] for column in _LABEL_COLUMN_ALIASES if row.get(column) is not None),
            None,
        )
        label = coerce_label(label_value)

        lang = str(row.get("lang_code") or default_lang_code).strip() or default_lang_code
        texts.append(text)
        labels.append(label)
        langs.append(lang)

    if not texts:
        raise ValueError(f"No usable rows found in {file_path}")

    logger.info("load_local_dataset: loaded %s examples from %s", len(texts), file_path)
    return Dataset.from_dict({"text": texts, "label": labels, "lang_code": langs})


def augment_dataset(
    dataset: Dataset,
    variants_per_example: int = 2,
    augment_fraction: float = 0.5,
    seed: int = 42,
    deduplicate: bool = True,
    class_augment_weights: Optional[Dict[int, float]] = None,
) -> Dataset:
    """
    Applique la recomposition EDA sur une fraction du dataset.

    La sélection des exemples à augmenter est pondérée par classe : les poids
    de `class_augment_weights` (label -> poids) augmentent préférentiellement
    la probabilité de sélection des exemples de la classe concernée. Par défaut,
    la classe neutral (label 1) est surpondérée pour compenser sa
    sous-représentation typique dans le corpus.

    Args:
        dataset: dataset HF avec colonnes 'text', 'label', 'lang_code'
        variants_per_example: nombre de variantes générées par texte
        augment_fraction: proportion du dataset à augmenter
        seed: graine aléatoire
        deduplicate: supprime les doublons de texte normalisé avant sampling
        class_augment_weights: dict optionnel {label: poids} pour sur-échantillonner
            préférentiellement certaines classes (ex. {1: 3.0} pour surreprésenter
            la classe neutral). None => surpoids par défaut sur la classe neutral.

    Returns:
        Dataset augmenté
    """
    from src.augmentation.eda import recompose

    random.seed(seed)
    df = dataset.to_pandas().copy()
    if deduplicate:
        seen_hashes = set()
        keep_mask = []
        removed_duplicates = 0

        for text in df["text"].fillna("").astype(str):
            normalized_text = text.strip().lower()
            text_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
            if text_hash in seen_hashes:
                removed_duplicates += 1
                keep_mask.append(False)
                continue
            seen_hashes.add(text_hash)
            keep_mask.append(True)

        if removed_duplicates:
            logger.info(
                "augment_dataset: removed %s duplicate texts before augmentation",
                removed_duplicates,
            )
            df = df.loc[keep_mask].reset_index(drop=True).copy()

    n_to_augment = int(len(df) * augment_fraction)
    if n_to_augment <= 0:
        return Dataset.from_pandas(df.reset_index(drop=True))

    # Normalise les poids (accepte des clés int ou str issues de YAML/JSON) et
    # bascule sur le surpoids par défaut de la classe neutral si rien n'est fourni.
    if class_augment_weights:
        weights = {int(k): float(v) for k, v in class_augment_weights.items()}
    else:
        weights = dict(DEFAULT_CLASS_AUGMENT_WEIGHTS)

    row_weights = df["label"].map(lambda lab: weights.get(lab, 1.0)).astype(float)
    # Échantillonnage pondéré SANS remplacement : pandas refuse de combiner
    # poids élevés + replace=False sur les petits datasets, on passe donc par
    # numpy qui renormalise les poids restants à chaque tirage.
    probs = row_weights.to_numpy(dtype=float)
    if probs.sum() <= 0:
        probs = None  # poids nuls partout => retombée sur un échantillonnage uniforme
    elif probs.sum() != 1.0:
        probs = probs / probs.sum()
    rng = np.random.RandomState(seed)
    selected_idx = rng.choice(len(df), size=n_to_augment, replace=False, p=probs)
    rows_to_augment = df.iloc[selected_idx]

    augmented_rows = []

    for _, row in rows_to_augment.iterrows():
        variants = recompose(
            row["text"],
            lang=row["lang_code"],
            num_variants=variants_per_example,
        )

        for v in variants:
            augmented_rows.append({
                "text": v,
                "label": row["label"],
                "lang_code": row["lang_code"],
            })

    augmented_df = pd.DataFrame(augmented_rows)
    full_df = pd.concat([df, augmented_df], ignore_index=True)
    full_df = full_df.sample(frac=1, random_state=seed).reset_index(drop=True)

    return Dataset.from_pandas(full_df)
