import io
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout

sys.modules.setdefault("src", types.ModuleType("src"))
sys.modules.setdefault("src.inference", types.ModuleType("src.inference"))
predictor_stub = types.ModuleType("src.inference.predictor")

class DummyPredictor:
    def __init__(self, *args, **kwargs):
        pass

    def predict(self, batch):
        return [{"text": text, "sentiment": "positive", "confidence": 0.9} for text in batch]

predictor_stub.Predictor = DummyPredictor
sys.modules["src.inference.predictor"] = predictor_stub

from label_dataset import build_alpaca_record, load_texts_from_file, main


class TestLabelDataset(unittest.TestCase):
    def test_load_texts_from_csv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            csv_path = os.path.join(tmpdir, "texts.csv")
            with open(csv_path, "w", encoding="utf-8") as f:
                f.write("text,meta\n")
                f.write('"J\'adore ce produit !",A\n')
                f.write('"C\'est correct.",B\n')

            rows = load_texts_from_file(csv_path, text_column="text")
            self.assertEqual(rows, ["J'adore ce produit !", "C'est correct."])

    def test_build_alpaca_record(self):
        record = build_alpaca_record(
            text="Super produit !",
            sentiment="positive",
            confidence=0.92,
        )

        self.assertEqual(record["instruction"], "Classify the sentiment of the following text as negative, neutral, or positive.")
        self.assertEqual(record["input"], "Super produit !")
        self.assertEqual(record["output"], "positive")
        self.assertEqual(record["confidence"], 0.92)

    def test_load_texts_from_jsonl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            jsonl_path = os.path.join(tmpdir, "texts.jsonl")
            with open(jsonl_path, "w", encoding="utf-8") as f:
                f.write(json.dumps({"text": "Tout va bien"}) + "\n")
                f.write(json.dumps({"text": "Très mauvais"}) + "\n")

            rows = load_texts_from_file(jsonl_path, text_column="text")
            self.assertEqual(rows, ["Tout va bien", "Très mauvais"])

    def test_cli_accepts_min_confidence_flag(self):
        original_argv = sys.argv[:]
        stdout = io.StringIO()
        try:
            sys.argv = [
                "label_dataset.py",
                "--input",
                "data/train.jsonl",
                "--output",
                "tmp_out.jsonl",
                "--min_confidence",
                "0.85",
            ]
            with redirect_stdout(stdout):
                main()
        finally:
            sys.argv = original_argv

        self.assertIn("Exported", stdout.getvalue())
