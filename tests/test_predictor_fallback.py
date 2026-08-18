import os
import tempfile

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from src.inference.predictor import Predictor


def test_predictor_loads_checkpoint_model_pt():
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

        assert len(result) == 1
        assert result[0]["sentiment"] in ["negative", "neutral", "positive"]
