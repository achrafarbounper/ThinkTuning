import csv
import io
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout

# Stub du module feuille src.inference.predictor AVANT d'importer
# active_learning, exactement comme dans tests/test_label_dataset.py, pour
# éviter de charger un vrai modèle DistilBERT pendant les tests.
#
# IMPORTANT : on n'injecte QUE le nom feuille (« src.inference.predictor »),
# jamais de faux modules parents (« src », « src.inference »). Un module nu
# créé via types.ModuleType() n'a pas de __path__ : s'il se retrouve dans
# sys.modules sous le nom « src », tout import ultérieur d'un sous-module
# (ex. api/__init__.py -> from src.utils.flags import TEST_MODE) échoue avec
# « No module named 'src.utils'; 'src' is not a package » pour le reste de
# la session pytest. setdefault garantit en plus qu'on n'écrase jamais un
# vrai module déjà importé par un autre test.
predictor_stub = types.ModuleType("src.inference.predictor")


class DummyPredictor:
    """Confidence dépendante du texte, pour vérifier le tri par incertitude."""

    def __init__(self, *args, **kwargs):
        pass

    def predict(self, batch):
        confidences = {
            "certain positive": 0.98,
            "certain negative": 0.95,
            "very uncertain": 0.34,
            "somewhat uncertain": 0.5,
        }
        results = []
        for text in batch:
            confidence = confidences.get(text, 0.7)
            results.append({"text": text, "sentiment": "positive", "confidence": confidence})
        return results


predictor_stub.Predictor = DummyPredictor
sys.modules.setdefault("src.inference.predictor", predictor_stub)

from active_learning import (
    compute_uncertainty,
    main,
    select_uncertain_examples,
    write_manual_review_csv,
)


class TestActiveLearning(unittest.TestCase):
    def test_compute_uncertainty_max_at_one_third(self):
        # Confidence == 1/3 -> incertitude maximale (distance nulle -> score = 1.0)
        self.assertAlmostEqual(compute_uncertainty(1 / 3), 1.0, places=5)

    def test_compute_uncertainty_lower_when_confident(self):
        self.assertLess(compute_uncertainty(0.95), compute_uncertainty(0.4))

    def test_select_uncertain_examples_sorted_descending(self):
        texts = ["certain positive", "very uncertain", "somewhat uncertain", "certain negative"]
        records = select_uncertain_examples(texts, model_path="dummy")

        uncertainties = [r["uncertainty"] for r in records]
        self.assertEqual(uncertainties, sorted(uncertainties, reverse=True))
        # "very uncertain" (confidence=0.34) est le plus proche de 1/3 -> doit être en tête
        self.assertEqual(records[0]["text"], "very uncertain")

    def test_select_uncertain_examples_respects_top_n(self):
        texts = ["certain positive", "very uncertain", "somewhat uncertain", "certain negative"]
        records = select_uncertain_examples(texts, model_path="dummy", top_n=2)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["text"], "very uncertain")

    def test_select_uncertain_examples_empty_input(self):
        self.assertEqual(select_uncertain_examples([], model_path="dummy"), [])

    def test_write_manual_review_csv(self):
        records = [
            {"text": "Exemple incertain", "predicted_label": "neutral", "confidence": 0.34, "uncertainty": 0.99},
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "review.csv")
            write_manual_review_csv(records, out_path)

            with open(out_path, encoding="utf-8") as f:
                reader = csv.DictReader(f)
                self.assertEqual(reader.fieldnames, ["text", "predicted_label", "manual_label", "status"])
                rows = list(reader)

            self.assertEqual(rows[0]["text"], "Exemple incertain")
            self.assertEqual(rows[0]["predicted_label"], "neutral")
            self.assertEqual(rows[0]["manual_label"], "")
            self.assertEqual(rows[0]["status"], "")

    def test_cli_writes_output_and_prints_summary(self):
        original_argv = sys.argv[:]
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            input_path = os.path.join(tmpdir, "texts.csv")
            with open(input_path, "w", encoding="utf-8") as f:
                f.write("text\n")
                f.write("very uncertain\n")
                f.write("certain positive\n")

            output_path = os.path.join(tmpdir, "manual_review_template.csv")
            try:
                sys.argv = [
                    "active_learning.py",
                    "--input", input_path,
                    "--output", output_path,
                    "--model_path", "dummy",
                ]
                with redirect_stdout(stdout):
                    main()
            finally:
                sys.argv = original_argv

            self.assertTrue(os.path.exists(output_path))

        self.assertIn("Exported", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
