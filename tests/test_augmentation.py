import random
import unittest

import pandas as pd
from datasets import Dataset

from src.augmentation.eda import random_swap, random_deletion, recompose
from src.dataset.loader import augment_dataset


class TestAugmentation(unittest.TestCase):
    def test_random_swap_preserves_length(self):
        words = ["un", "deux", "trois"]
        swapped = random_swap(words, n=1)
        self.assertEqual(len(swapped), len(words))
        self.assertEqual(set(swapped), set(words))

    def test_random_deletion_never_returns_empty(self):
        words = ["un", "deux"]
        deleted = random_deletion(words, p=1.0)
        self.assertTrue(len(deleted) >= 1)
        self.assertTrue(all(w in words for w in deleted))

    def test_recompose_returns_expected_variants(self):
        random.seed(42)
        variants = recompose("Bonjour le monde", lang="fr", num_variants=2, alpha=0.5)
        self.assertEqual(len(variants), 2)
        self.assertNotEqual(variants[0], "Bonjour le monde")
        self.assertNotEqual(variants[1], "Bonjour le monde")

    def test_augment_dataset_deduplicates_before_eda(self):
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
        self.assertEqual(len(unique_normalized), 3)
        self.assertEqual(len(augmented), 3 + (3 * 3))

        generated_variants = {
            row["text"]
            for row in augmented
            if row["text"] not in {"J'aime vraiment ce film", "C'est un film très mauvais", "Autre phrase neutre pour tester"}
        }
        self.assertGreater(len(generated_variants), 1)
        self.assertTrue(any(row["text"] != "J'aime vraiment ce film" for row in augmented))
        self.assertTrue(any(row["text"] != "C'est un film très mauvais" for row in augmented))
