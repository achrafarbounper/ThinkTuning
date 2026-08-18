import os

import pytest

from src.inference.predictor import Predictor


def test_predictor_loads_and_predicts():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    model_path = os.path.join(root, "sentiment_model_final")

    if not os.path.isdir(model_path):
        pytest.skip(f"Model directory not found: {model_path}")

    predictor = Predictor(model_path)
    results = predictor.predict(["Ce produit est fantastique, je recommande !"])

    assert len(results) == 1
    assert "sentiment" in results[0]
    assert results[0]["sentiment"] in ["negative", "neutral", "positive"]
    assert "confidence" in results[0]
    assert isinstance(results[0]["confidence"], float)
    assert results[0]["confidence"] >= 0.0
    assert results[0]["confidence"] <= 1.0


def test_predictor_predicts_multiple_texts():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    model_path = os.path.join(root, "sentiment_model_final")

    if not os.path.isdir(model_path):
        pytest.skip(f"Model directory not found: {model_path}")

    predictor = Predictor(model_path)
    texts = [
        "Ce produit est fantastique, je recommande !",
        "Je ne suis pas du tout satisfait de cet achat.",
        "C'est correct, sans plus.",
    ]
    results = predictor.predict(texts)

    assert len(results) == len(texts)
    for text, result in zip(texts, results):
        assert result["text"] == text
        assert result["sentiment"] in ["negative", "neutral", "positive"]
        assert result["confidence"] >= 0.0
        assert result["confidence"] <= 1.0


"""def test_predictor_raises_on_missing_model_path():
    missing_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "missing_model")
    with pytest.raises(FileNotFoundError):
        Predictor(missing_path)


def test_predictor_falls_back_when_tokenizer_missing(tmp_path, monkeypatch):
    import src.inference.predictor as predictor_module

    class DummyModel:
        def eval(self):
            pass

        def load_state_dict(self, *args, **kwargs):
            return None

    def fake_tokenizer_from_pretrained(path, *args, **kwargs):
        if str(path) == str(tmp_path):
            raise OSError("Tokenizer files not found in model directory")
        return object()

    def fake_model_from_pretrained(path, *args, **kwargs):
        if str(path) == str(tmp_path):
            raise OSError("Model config files not found in model directory")
        return DummyModel()

    monkeypatch.setattr(predictor_module.AutoTokenizer, "from_pretrained", fake_tokenizer_from_pretrained)
    monkeypatch.setattr(
        predictor_module.AutoModelForSequenceClassification,
        "from_pretrained",
        fake_model_from_pretrained,
    )

    with pytest.raises(FileNotFoundError, match="model.pt"):
        Predictor(str(tmp_path))"""
