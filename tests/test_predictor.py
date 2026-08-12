import os
import unittest

from src.inference.predictor import Predictor


class TestPredictor(unittest.TestCase):
    def test_predictor_loads_and_predicts(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        model_path = os.path.join(root, "sentiment_model_final")

        if not os.path.isdir(model_path):
            self.skipTest(f"Model directory not found: {model_path}")

        predictor = Predictor(model_path)
        results = predictor.predict(["Ce produit est fantastique, je recommande !"])

        self.assertEqual(len(results), 1)
        self.assertIn("sentiment", results[0])
        self.assertIn(results[0]["sentiment"], ["negative", "neutral", "positive"])
        self.assertIn("confidence", results[0])
        self.assertIsInstance(results[0]["confidence"], float)
        self.assertGreaterEqual(results[0]["confidence"], 0.0)
        self.assertLessEqual(results[0]["confidence"], 1.0)

    def test_predictor_predicts_multiple_texts(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        model_path = os.path.join(root, "sentiment_model_final")

        if not os.path.isdir(model_path):
            self.skipTest(f"Model directory not found: {model_path}")

        predictor = Predictor(model_path)
        texts = [
            "Ce produit est fantastique, je recommande !",
            "Je ne suis pas du tout satisfait de cet achat.",
            "C'est correct, sans plus.",
        ]
        results = predictor.predict(texts)

        self.assertEqual(len(results), len(texts))
        for text, result in zip(texts, results):
            self.assertEqual(result["text"], text)
            self.assertIn(result["sentiment"], ["negative", "neutral", "positive"])
            self.assertGreaterEqual(result["confidence"], 0.0)
            self.assertLessEqual(result["confidence"], 1.0)

    def test_predictor_raises_on_missing_model_path(self):
        missing_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "missing_model")
        with self.assertRaises(FileNotFoundError):
            Predictor(missing_path)
