import unittest

from predict_llm import parse_generation


class PredictLLMTest(unittest.TestCase):
    def test_parse_generation_positive(self):
        result = parse_generation("positive\nconfidence: 0.94")
        self.assertEqual(result["sentiment"], "positive")
        self.assertAlmostEqual(result["confidence"], 0.94, places=3)

    def test_parse_generation_negative_french(self):
        result = parse_generation("négatif\nconfiance: 0.81")
        self.assertEqual(result["sentiment"], "negative")
        self.assertAlmostEqual(result["confidence"], 0.81, places=3)


if __name__ == "__main__":
    unittest.main()
