"""
Prédiction de sentiment sur un texte (français ou anglais).

Usage :
    python predict.py "Ce produit est fantastique !"
"""

import logging
import sys
from src.inference.predictor import Predictor

logger = logging.getLogger(__name__)

MODEL_PATH = "./sentiment_model_final"


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    if len(sys.argv) > 1:
        texts = [sys.argv[1]]
    else:
        texts = [
            "Ce film était vraiment excellent, j'ai adoré chaque instant.",
            "This product is terrible, I want a refund.",
            "C'était correct, sans plus.",
        ]

    predictor = Predictor(MODEL_PATH)
    results = predictor.predict(texts)

    for r in results:
        logger.info(f"[{r['sentiment'].upper():>8}] ({r['confidence']:.1%}) -> {r['text']}")


if __name__ == "__main__":
    main()
