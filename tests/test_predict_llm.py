import pytest

from predict_llm import parse_generation


def test_parse_generation_positive():
    result = parse_generation("positive\nconfidence: 0.94")
    assert result["sentiment"] == "positive"
    assert result["confidence"] == pytest.approx(0.94, abs=0.001)


def test_parse_generation_negative_french():
    result = parse_generation("négatif\nconfiance: 0.81")
    assert result["sentiment"] == "negative"
    assert result["confidence"] == pytest.approx(0.81, abs=0.001)
