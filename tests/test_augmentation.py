import random
import unittest

from src.augmentation.eda import random_swap, random_deletion, recompose


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
