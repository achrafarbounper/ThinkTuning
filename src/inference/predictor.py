import os

import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

from src.dataset.loader import LABEL_NAMES

_DEFAULT_MAX_LENGTH = 128


def _resolve_default_max_length():
    """
    Essaie de lire max_length depuis configs/default.yaml pour rester
    cohérent avec la troncature utilisée à l'entraînement. Retombe sur
    128 si le fichier de config est introuvable ou illisible.
    """
    try:
        from src.utils.config import load_config
        cfg = load_config("configs/default.yaml")
        return cfg.get("max_length", _DEFAULT_MAX_LENGTH)
    except Exception:
        return _DEFAULT_MAX_LENGTH


class Predictor:
    """
    Charge un modèle entraîné et effectue des prédictions
    multilingues (fr/en) sur des textes.
    """

    def __init__(self, model_path: str, max_length: int = None):
        self.model_path = model_path
        self.model_name = "distilbert-base-multilingual-cased"
        # Doit correspondre au max_length utilisé à l'entraînement pour que
        # la troncature soit identique entre entraînement et inférence.
        self.max_length = max_length if max_length is not None else _resolve_default_max_length()

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
            max_length=self.max_length,
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
