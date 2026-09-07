"""
Prédiction de sentiment sur un texte (français ou anglais).

Usage :
    python predict.py "Ce produit est fantastique !"
"""

import logging
import sys
from core.model_versioning import resolve_model_path
from src.inference.predictor import Predictor

logger = logging.getLogger(__name__)

# Par défaut : dernière version valide dans experiments/models (résout aussi
# l'ancien chemin ./sentiment_model_final vers experiments/models).
MODEL_PATH = resolve_model_path(None)


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

    logger.info(f"Modèle utilisé : {MODEL_PATH}")
    predictor = Predictor(MODEL_PATH)
    results = predictor.predict(texts)

    for r in results:
        logger.info(f"[{r['sentiment'].upper():>8}] ({r['confidence']:.1%}) -> {r['text']}")


if __name__ == "__main__":
    main()
