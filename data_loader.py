"""
Chargement d'un dataset multilingue de sentiments (français + anglais)
et application du système de recomposition pour l'augmenter.

Dataset utilisé : "cardiffnlp/tweet_sentiment_multilingual" (Hugging Face Hub)
-> chargé directement depuis les fichiers Parquet auto-convertis par HF
   (évite l'erreur "Dataset scripts are no longer supported" des versions
   récentes de la librairie `datasets`, ce dataset utilisant historiquement
   un script de chargement .py).
   Contient français, anglais + 6 autres langues, labels :
   0 = negative, 1 = neutral, 2 = positive.
"""

import random
from typing import Optional

import pandas as pd
from datasets import load_dataset, Dataset, concatenate_datasets

from augmentation import recompose

LABEL_NAMES = {0: "negative", 1: "neutral", 2: "positive"}

# Fichiers Parquet auto-générés par Hugging Face à partir du dataset original
# (contourne le script de chargement .py, non supporté par les versions
# récentes de `datasets`).
_PARQUET_BASE = (
    "https://huggingface.co/datasets/cardiffnlp/tweet_sentiment_multilingual"
    "/resolve/refs%2Fconvert%2Fparquet"
)
_LANG_CONFIG = {"fr": "french", "en": "english"}


def load_raw_dataset(languages=("fr", "en"), max_per_lang: Optional[int] = 3000):
    """Charge et concatène les sous-ensembles de langues demandées."""
    subsets = []
    for lang_code in languages:
        config = _LANG_CONFIG[lang_code]
        data_files = {"train": f"{_PARQUET_BASE}/{config}/train/0000.parquet"}
        ds = load_dataset("parquet", data_files=data_files, split="train")
        if max_per_lang:
            ds = ds.shuffle(seed=42).select(range(min(max_per_lang, len(ds))))
        ds = ds.add_column("lang_code", [lang_code] * len(ds))
        subsets.append(ds)
    return concatenate_datasets(subsets)


def augment_dataset(dataset: Dataset, variants_per_example: int = 2,
                     augment_fraction: float = 0.5, seed: int = 42) -> Dataset:
    """
    Applique le système de recomposition sur une fraction du dataset
    pour générer des exemples supplémentaires.

    Args:
        dataset: dataset Hugging Face avec colonnes 'text', 'label', 'lang_code'
        variants_per_example: nombre de variantes générées par texte augmenté
        augment_fraction: proportion du dataset à augmenter
        seed: graine aléatoire

    Returns:
        Dataset original + exemples recomposés
    """
    random.seed(seed)
    df = dataset.to_pandas()

    n_to_augment = int(len(df) * augment_fraction)
    rows_to_augment = df.sample(n=n_to_augment, random_state=seed)

    augmented_rows = []
    for _, row in rows_to_augment.iterrows():
        variants = recompose(
            row["text"], lang=row["lang_code"], num_variants=variants_per_example
        )
        for v in variants:
            augmented_rows.append({
                "text": v,
                "label": row["label"],
                "lang_code": row["lang_code"],
            })

    augmented_df = pd.DataFrame(augmented_rows)
    full_df = pd.concat([df, augmented_df], ignore_index=True)
    full_df = full_df.sample(frac=1, random_state=seed).reset_index(drop=True)  # shuffle

    return Dataset.from_pandas(full_df)


if __name__ == "__main__":
    print("Chargement du dataset multilingue (fr + en)...")
    raw = load_raw_dataset(max_per_lang=200)  # petit échantillon pour test rapide
    print(f"Dataset brut : {len(raw)} exemples")

    print("\nApplication du système de recomposition (augmentation)...")
    augmented = augment_dataset(raw, variants_per_example=2, augment_fraction=0.3)
    print(f"Dataset augmenté : {len(augmented)} exemples")

    print("\nExemple avant/après :")
    print(raw[0])
