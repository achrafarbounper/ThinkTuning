"""
Chargement d'un dataset multilingue (français + anglais) pour la classification
de sentiments, avec possibilité d'appliquer une augmentation EDA.

Dataset : cardiffnlp/tweet_sentiment_multilingual
Chargé via les fichiers Parquet auto-convertis par Hugging Face.
"""

import hashlib
import logging
import random
from typing import Optional, Iterable

import pandas as pd
from datasets import load_dataset, Dataset, concatenate_datasets

logger = logging.getLogger(__name__)

LABEL_NAMES = {0: "negative", 1: "neutral", 2: "positive"}

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


def augment_dataset(
    dataset: Dataset,
    variants_per_example: int = 2,
    augment_fraction: float = 0.5,
    seed: int = 42,
    deduplicate: bool = True,
) -> Dataset:
    """
    Applique la recomposition EDA sur une fraction du dataset.

    Args:
        dataset: dataset HF avec colonnes 'text', 'label', 'lang_code'
        variants_per_example: nombre de variantes générées par texte
        augment_fraction: proportion du dataset à augmenter
        seed: graine aléatoire
        deduplicate: supprime les doublons de texte normalisé avant sampling

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

    rows_to_augment = df.sample(n=n_to_augment, random_state=seed)

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
