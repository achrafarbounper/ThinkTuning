import random

import pandas as pd
from datasets import Dataset

from src.augmentation.eda import random_swap, random_deletion, recompose
from src.dataset.loader import augment_dataset


def test_random_swap_preserves_length():
    words = ["un", "deux", "trois"]
    swapped = random_swap(words, n=1)
    assert len(swapped) == len(words)
    assert set(swapped) == set(words)


def test_random_deletion_never_returns_empty():
    words = ["un", "deux"]
    deleted = random_deletion(words, p=1.0)
    assert len(deleted) >= 1
    assert all(w in words for w in deleted)


def test_recompose_returns_expected_variants():
    random.seed(42)
    variants = recompose("Bonjour le monde", lang="fr", num_variants=2, alpha=0.5)
    assert len(variants) == 2
    assert variants[0] != "Bonjour le monde"
    assert variants[1] != "Bonjour le monde"


def test_augment_dataset_deduplicates_before_eda():
    data = pd.DataFrame(
        {
            "text": [
                "J'aime vraiment ce film",
                "  J'AIME VRAIMENT CE FILM  ",
                "C'est un film très mauvais",
                "C'est un film très mauvais",
                "Autre phrase neutre pour tester",
            ],
            "label": [1, 1, 0, 0, 2],
            "lang_code": ["fr", "fr", "fr", "fr", "fr"],
        }
    )
    dataset = Dataset.from_pandas(data)

    augmented = augment_dataset(
        dataset,
        variants_per_example=3,
        augment_fraction=1.0,
        seed=42,
        deduplicate=True,
    )

    original_texts = [row["text"] for row in dataset]
    unique_normalized = {
        text.strip().lower() for text in original_texts
    }
    assert len(unique_normalized) == 3
    assert len(augmented) == 3 + (3 * 3)

    generated_variants = {
        row["text"]
        for row in augmented
        if row["text"] not in {"J'aime vraiment ce film", "C'est un film très mauvais", "Autre phrase neutre pour tester"}
    }
    assert len(generated_variants) > 1
    assert any(row["text"] != "J'aime vraiment ce film" for row in augmented)
    assert any(row["text"] != "C'est un film très mauvais" for row in augmented)


def test_augment_dataset_targets_neutral_class_by_default():
    """Sans class_augment_weights, la classe neutral (label 1) est sur-représentée."""
    n_neutral, n_negative = 8, 40
    data = pd.DataFrame(
        {
            "text": [f"phrase neutre numéro {i} sans émotion forte" for i in range(n_neutral)]
            + [f"phrase négative numéro {i} film vraiment mauvais" for i in range(n_negative)],
            "label": [1] * n_neutral + [0] * n_negative,
            "lang_code": ["fr"] * (n_neutral + n_negative),
        }
    )
    dataset = Dataset.from_pandas(data)
    original_neutral_frac = sum(l == 1 for l in dataset["label"]) / len(dataset)

    augmented = augment_dataset(
        dataset,
        variants_per_example=3,
        augment_fraction=0.5,
        seed=42,
    )

    neutral_frac = sum(1 for row in augmented if row["label"] == 1) / len(augmented)
    assert neutral_frac > original_neutral_frac


def test_augment_dataset_class_augment_weights_only_selects_neutral():
    """Un poids nul sur les autres classes force la sélection d'exemples neutral."""
    n_neutral, n_negative = 40, 40
    data = pd.DataFrame(
        {
            "text": [f"texte neutre numéro {i} sans jugement" for i in range(n_neutral)]
            + [f"texte négatif numéro {i} très décevant" for i in range(n_negative)],
            "label": [1] * n_neutral + [0] * n_negative,
            "lang_code": ["fr"] * (n_neutral + n_negative),
        }
    )
    dataset = Dataset.from_pandas(data)

    # Rows sélectionnées pour l'augmentation = uniquement des neutral (label 1).
    augmented = augment_dataset(
        dataset,
        variants_per_example=1,
        augment_fraction=0.5,  # 40 samples parmi 80 -> tous parmi les 40 neutral
        seed=42,
        class_augment_weights={0: 0.0, 1: 1.0, 2: 0.0},
    )

    # Chaque variante générée (nouveau texte) doit porter le label neutral.
    original_texts = set(row["text"] for row in dataset)
    for row in augmented:
        if row["text"] not in original_texts:
            assert row["label"] == 1
