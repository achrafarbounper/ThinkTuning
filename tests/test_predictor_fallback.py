import os
import tempfile
import unittest

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from src.inference.predictor import Predictor


class TestPredictorFallback(unittest.TestCase):
    def test_predictor_loads_checkpoint_model_pt(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tokenizer = AutoTokenizer.from_pretrained("distilbert-base-multilingual-cased")
            model = AutoModelForSequenceClassification.from_pretrained(
                "distilbert-base-multilingual-cased",
                num_labels=3,
            )
            tokenizer.save_pretrained(tmpdir)
            model.save_pretrained(tmpdir)

            predictor = Predictor(tmpdir)
            result = predictor.predict(["Test rapide"])

            self.assertEqual(len(result), 1)
            self.assertIn(result[0]["sentiment"], ["negative", "neutral", "positive"])
