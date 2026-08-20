"""Tests pour benchmark.py (sans chargement de modèles ni GPU).

Les stubs de modules sont installés PUIS restaurés via `monkeypatch` au niveau
de chaque test concerné, pour ne pas polluer ``sys.modules`` (et donc ne pas
casser d'autres tests comme test_predict_llm.py).
"""
import io
import json
import os
import sys
import tempfile
import types
from contextlib import redirect_stdout

import benchmark
from benchmark import (
    compute_metrics,
    load_records,
    normalize_label,
    resolve_prediction,
)


def _install_predictor_stub(monkeypatch):
    """Installe un stub pour src.inference.predictor.Predictor (CPU-safe)."""
    module = types.ModuleType("src.inference.predictor")

    class DummyPredictor:
        def __init__(self, *args, **kwargs):
            self.model = self

        def to(self, *args, **kwargs):
            return self

        def eval(self):
            return self

        def predict(self, batch):
            return [
                {"text": t, "sentiment": "positive", "confidence": 0.9} for t in batch
            ]

    module.Predictor = DummyPredictor
    monkeypatch.setitem(sys.modules, "src.inference.predictor", module)


def _install_llm_stub(monkeypatch):
    """Installe un stub pour le module predict_llm (optionnel)."""
    module = types.ModuleType("predict_llm")

    class DummyLLM:
        def to(self, *args, **kwargs):
            return self

        def eval(self):
            return self

    module.load_model = lambda *a, **k: DummyLLM()
    module.load_tokenizer = lambda *a, **k: object()
    module.predict_text = lambda m, t, text, max_new_tokens=0: {
        "text": text,
        "sentiment": "negative",
        "confidence": 0.7,
    }
    monkeypatch.setitem(sys.modules, "predict_llm", module)


def test_normalize_label_french_and_english():
    assert normalize_label("négatif") == "negative"
    assert normalize_label("Négatif") == "negative"
    assert normalize_label("positif") == "positive"
    assert normalize_label("neutre") == "neutral"
    assert normalize_label("good") == "positive"
    assert normalize_label("terrible") == "negative"
    assert normalize_label("") == "unknown"
    assert normalize_label(None) == "unknown"


def test_resolve_prediction_forces_wrong_on_unknown():
    # une prédiction 'unknown' doit donner une classe différente du gold
    assert resolve_prediction("unknown", "negative") != "negative"
    assert resolve_prediction("unknown", "positive") != "positive"
    # une prédiction interprétable est renvoyée telle quelle
    assert resolve_prediction("positive", "negative") == "positive"


def test_compute_metrics_perfect():
    golds = ["positive", "negative", "neutral", "positive"]
    preds = ["positive", "negative", "neutral", "positive"]
    metrics = compute_metrics(preds, golds)
    assert metrics["accuracy"] == 1.0
    assert metrics["f1_macro"] == 1.0
    assert metrics["f1_negative"] == 1.0
    assert metrics["f1_neutral"] == 1.0
    assert metrics["f1_positive"] == 1.0


def test_compute_metrics_counts_unknown_as_error():
    golds = ["positive", "positive", "positive"]
    preds = ["positive", "unknown", "neutral"]  # 1 correct / 2 incorrects
    metrics = compute_metrics(preds, golds)
    assert metrics["accuracy"] == 1.0 / 3.0
    for key in ("f1_negative", "f1_neutral", "f1_positive"):
        assert key in metrics


def test_load_records_jsonl_alpaca():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "val.jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"input": "J'adore", "output": "positive"}) + "\n")
            f.write(json.dumps({"input": "Déçu", "output": "negative"}) + "\n")
            f.write(json.dumps({"input": "", "output": "positive"}) + "\n")  # ignoré
        rows = load_records(path, text_column="text", label_column="label")
    assert rows == [("J'adore", "positive"), ("Déçu", "negative")]


def test_load_records_csv():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "val.csv")
        with open(path, "w", encoding="utf-8") as f:
            f.write("text,label\n")
            f.write("Top produit,positive\n")
            f.write("Très mauvais,negative\n")
        rows = load_records(path, text_column="text", label_column="label")
    assert rows == [("Top produit", "positive"), ("Très mauvais", "negative")]


def test_measure_engine_with_runner(monkeypatch):
    _install_predictor_stub(monkeypatch)
    runner = benchmark.create_distilbert_runner("fake-model", "cpu", 2)
    measured = benchmark.measure_engine(runner, ["a", "b", "c"])
    assert measured["n_predictions"] == 3
    assert measured["latency_per_text_ms"] >= 0.0
    assert measured["rss_mb"] > 0.0
    assert len(measured["predicted_labels"]) == 3


def test_main_writes_report(tmp_path, monkeypatch):
    _install_predictor_stub(monkeypatch)
    _install_llm_stub(monkeypatch)

    test_jsonl = tmp_path / "val.jsonl"
    with open(test_jsonl, "w", encoding="utf-8") as f:
        f.write(json.dumps({"input": "Très content", "output": "positive"}) + "\n")
        f.write(json.dumps({"input": "mitigé", "output": "neutral"}) + "\n")

    out_json = tmp_path / "report.json"
    stdout = io.StringIO()
    argv = [
        "--distilbert_path",
        "fake-model",
        "--llm_path",
        "fake-llm",
        "--test_file",
        str(test_jsonl),
        "--output",
        str(out_json),
        "--max_texts",
        "2",
    ]
    with redirect_stdout(stdout):
        report = benchmark.main(argv)

    assert os.path.exists(out_json)
    assert "distilbert" in report["models"]
    assert "llm" in report["models"]
    assert report["dataset"]["n_evaluated"] == 2
    with open(out_json, "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert "models" in saved
    assert all(
        k in saved["models"]["distilbert"]
        for k in ("metrics", "latency_per_text_ms", "rss_mb")
    )