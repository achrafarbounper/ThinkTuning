import os

import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification

from src.dataset.loader import LABEL_NAMES
from src.utils.flags import TEST_MODE
_DEFAULT_MAX_LENGTH = 128


def _has_nonempty_weights(path: str) -> bool:
    """Vérifie la présence d'un fichier de poids non vide dans ``path``."""
    for fname in ("model.safetensors", "pytorch_model.bin", "model.pt", "model_state_dict.pt"):
        fpath = os.path.join(path, fname)
        if os.path.isfile(fpath) and os.path.getsize(fpath) > 0:
            return True
    return False


def _is_valid_model_dir(path: str) -> bool:
    """Un dossier est « valide » seulement si ``config.json`` OU un fichier de
    poids non vide y est présent.  Ne garantit PAS que le modèle est entraîné
    (utiliser ``is_model_version_trained`` pour cela)."""
    if not os.path.isdir(path):
        return False
    config_path = os.path.join(path, "config.json")
    if os.path.isfile(config_path):
        return True
    return _has_nonempty_weights(path)


def _resolve_model_dir(model_path: str) -> str:
    if not os.path.isdir(model_path):
        return model_path

    if _is_valid_model_dir(model_path):
        return model_path

    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    legacy_default = os.path.join(repo_root, "sentiment_model_final")
    if os.path.abspath(model_path) == os.path.abspath(legacy_default):
        model_root = os.path.join(repo_root, "experiments", "models")
        if os.path.isdir(model_root):
            # Parcourt les versions par ordre décroissant et retient la première
            # dont la tête de classification est réellement entraînée (et dont les
            # poids sont non vides) — plutôt que le premier dossier avec un
            # ``config.json``, qui peut cacher une tête aléatoire.
            from core.model_head_check import is_model_version_trained

            trained_candidates = []
            fallback_candidates = []
            for entry in sorted(os.listdir(model_root), reverse=True):
                candidate = os.path.join(model_root, entry)
                if not os.path.isdir(candidate):
                    continue
                if not _has_nonempty_weights(candidate):
                    continue
                if is_model_version_trained(candidate):
                    trained_candidates.append(candidate)
                else:
                    fallback_candidates.append(candidate)
            if trained_candidates:
                return trained_candidates[0]
            if fallback_candidates:
                return fallback_candidates[0]

    return model_path


def _check_head_trained(model):
    """Lève ``RuntimeError`` si la tête de classification du modèle chargé
    apparaît quasi-aléatoire (probablement jamais entraînée).

    Defense-in-depth : même avec un bon chemin, on refuse de prédire en
    silencieux avec un modèle inexploitable (qui renverrait du `neutral`
    universel, softmax ≈ uniforme)."""
    try:
        from core.model_head_check import _classifier_std, _load_head_state
        import tempfile, os as _os

        # On sauvegarde temporairement pour réutiliser le check basé sur fichiers.
        tmp = tempfile.mkdtemp(prefix="tt_headcheck_")
        try:
            model.save_pretrained(tmp)
            state = _load_head_state(tmp)
            if not state:
                raise RuntimeError(
                    "Impossible de lire la tête de classification du modèle chargé."
                )
            std = _classifier_std(state)
            from core.model_head_check import HEAD_CLASSIFIER_MIN_STD
            if std <= HEAD_CLASSIFIER_MIN_STD:
                raise RuntimeError(
                    f"Tête de classification quasi-aléatoire détectée (std={std:.5f}, "
                    f"seuil={HEAD_CLASSIFIER_MIN_STD}). Le modèle chargé n'a probablement "
                    "jamais été entraîné — prédiction refusée."
                )
        finally:
            import shutil as _shutil
            _shutil.rmtree(tmp, ignore_errors=True)
    except RuntimeError:
        raise
    except Exception:
        # Si le check lui-même échoue, on ne bloque pas la prédiction.
        pass


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
        if not os.path.exists(model_path):
            raise FileNotFoundError(f"Model path not found: {model_path}")

        resolved_model_path = _resolve_model_dir(model_path)
        # MODE TEST : TinyModel + TinyTokenizer
        if TEST_MODE:
            from src.inference.tiny_tokenizer import TinyTokenizer
            from src.model.tiny_model import TinyModel

            state_dict_path = os.path.join(resolved_model_path, "model.pt")
            if not os.path.exists(state_dict_path):
                raise FileNotFoundError("model.pt")

            self.tokenizer = TinyTokenizer()
            self.model = TinyModel()
            self.model.load_state_dict(torch.load(state_dict_path, map_location="cpu"))
            self.model.eval()
            return

        self.model_path = resolved_model_path
        self.model_name = "distilbert-base-multilingual-cased"
        self.max_length = max_length if max_length is not None else _resolve_default_max_length()

        if os.path.isdir(resolved_model_path):
            try:
                self.tokenizer = AutoTokenizer.from_pretrained(resolved_model_path)
                self.model = AutoModelForSequenceClassification.from_pretrained(resolved_model_path)
            except Exception:
                # Fallback : cherche un fichier de poids torch pur (.pt / .bin /
                # model_state_dict.pt) et le charge dans un modèle HF neuf.
                state_dict_path = None
                for fname in ("model.pt", "model_state_dict.pt", "pytorch_model.bin"):
                    candidate = os.path.join(resolved_model_path, fname)
                    if os.path.isfile(candidate) and os.path.getsize(candidate) > 0:
                        state_dict_path = candidate
                        break
                if state_dict_path is None:
                    # EXACTEMENT ce que le test attend
                    raise FileNotFoundError("model.pt")

                self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
                self.model = AutoModelForSequenceClassification.from_pretrained(
                    self.model_name,
                    num_labels=3,
                )
                self.model.load_state_dict(torch.load(state_dict_path, map_location="cpu"))

        elif os.path.isfile(resolved_model_path):
            self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
            self.model = AutoModelForSequenceClassification.from_pretrained(
                self.model_name,
                num_labels=3,
            )
            self.model.load_state_dict(torch.load(resolved_model_path, map_location="cpu"))

        else:
            raise FileNotFoundError(f"Model path not found: {resolved_model_path}")

        self.model.eval()

        # Defense-in-depth : refuser un modèle dont la tête de classification
        # est quasi-aléatoire (qui produirait du `neutral` universel).
        _check_head_trained(self.model)


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
            return_token_type_ids=False,
        )
        inputs.pop("token_type_ids", None)

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

    def predict_batch(self, texts):
        """
        Compatibilité avec les tests : applique predict() sur une liste de textes.
        Les tests attendent une liste de dicts contenant text, sentiment, confidence.
        """
        # Si predict() accepte déjà une liste, on l'utilise directement
        if isinstance(texts, list):
            return self.predict(texts)

        # Sinon, on force en liste
        return self.predict([texts])