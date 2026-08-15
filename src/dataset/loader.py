"""
Chargement d'un dataset multilingue (français + anglais) pour la classification
de sentiments, avec possibilité d'appliquer une augmentation EDA.

Dataset : cardiffnlp/tweet_sentiment_multilingual
Chargé via les fichiers Parquet auto-convertis par Hugging Face.
"""

import random
from typing import Optional, Iterable

import pandas as pd
from datasets import load_dataset, Dataset, concatenate_datasets

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
) -> Dataset:
    """
    Applique la recomposition EDA sur une fraction du dataset.

    Args:
        dataset: dataset HF avec colonnes 'text', 'label', 'lang_code'
        variants_per_example: nombre de variantes générées par texte
        augment_fraction: proportion du dataset à augmenter
        seed: graine aléatoire

    Returns:
        Dataset augmenté
    """
    from src.augmentation.eda import recompose

    random.seed(seed)
    df = dataset.to_pandas()

    n_to_augment = int(len(df) * augment_fraction)
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
