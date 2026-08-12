import os

import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

from src.dataset.loader import LABEL_NAMES


class Predictor:
    """
    Charge un modèle entraîné et effectue des prédictions
    multilingues (fr/en) sur des textes.
    """

    def __init__(self, model_path: str):
        self.model_path = model_path
        self.model_name = "distilbert-base-multilingual-cased"

        if os.path.isdir(model_path):
            try:
                self.tokenizer = AutoTokenizer.from_pretrained(model_path)
                self.model = AutoModelForSequenceClassification.from_pretrained(model_path)
            except ValueError:
                self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
                self.model = AutoModelForSequenceClassification.from_pretrained(
                    self.model_name,
                    num_labels=3,
                )
                state_dict_path = os.path.join(model_path, "model.pt")
                if os.path.exists(state_dict_path):
                    self.model.load_state_dict(torch.load(state_dict_path, map_location="cpu"))
                else:
                    raise
        elif os.path.isfile(model_path):
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
            self.model = AutoModelForSequenceClassification.from_pretrained(
                self.model_name,
                num_labels=3,
            )
            self.model.load_state_dict(torch.load(model_path, map_location="cpu"))
        else:
            raise FileNotFoundError(f"Model path not found: {model_path}")

        self.model.eval()

    def predict(self, texts):
        """
        Prédit le sentiment d'une liste de textes.

        Args:
            texts: liste de chaînes de caractères

        Returns:
            Liste de dicts : {text, sentiment, confidence}
        """
        inputs = self.tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=128,
            return_tensors="pt",
        )

        with torch.no_grad():
            logits = self.model(**inputs).logits

        probs = torch.softmax(logits, dim=-1)
        preds = torch.argmax(probs, dim=-1)

        results = []
        for text, pred, prob in zip(texts, preds, probs):
            results.append({
                "text": text,
                "sentiment": LABEL_NAMES[pred.item()],
                "confidence": round(prob[pred].item(), 3),
            })

        return results
