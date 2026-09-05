"""Export PyTorch -> ONNX et moteur d'inférence ONNX Runtime (Phase 3).

Le pipeline reste inchangé par défaut (``SentimentClassifier``/``Predictor``
en PyTorch) ; l'export ONNX est une optimisation additive : même tokenizer,
mêmes logits, même post-traitement. ``ONNXClassificationEngine`` charge un
modèle ONNX exporté (voir ``scripts/export_onnx.py``) et expose un contrat
``predict(list[str]) -> list[dict]`` compatible avec le reste du dépôt.

``onnxruntime`` et ``torch`` ne sont importés que dans les fonctions qui en
ont besoin : ce module reste importable (et testable avec des raccourcis)
dans des environnements sans runtime.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger("thinktuning.core.onnx_exporter")

DEFAULT_OPSET = 14


def export_model_to_onnx(
    model: Any,
    tokenizer: Any,
    output_path: str | Path,
    *,
    opset_version: int = DEFAULT_OPSET,
    probe_text: str = "Texte de contrôle pour l'export ONNX.",
) -> Path:
    """Exporte un modèle de classification HuggingFace vers ONNX.

    Args:
        model: modèle PyTorch ``AutoModelForSequenceClassification`` (eval) ;
        tokenizer: tokenizer associé (``AutoTokenizer``) ;
        output_path: chemin du fichier ``.onnx`` à créer (ou écraser) ;
        opset_version: version du toolkit ONNX (14 = large compatibilité) ;
        probe_text: phrase de sonde pour la trace du graphe.

    Returns:
        Le chemin résolu du fichier ONNX créé.

    Raises:
        RuntimeError: si ``torch`` ou ``onnx`` est indisponible.
    """
    try:
        import torch
    except ImportError as exc:  # pragma: no cover - environnement minimal
        raise RuntimeError("torch est requis pour exporter un modèle ONNX.") from exc

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    model.eval()
    with torch.inference_mode():
        sample = tokenizer(probe_text, return_tensors="pt")
        input_names = list(sample.keys())
        dynamic_axes: dict[str, dict[int, str]] = {
            name: {0: "batch", 1: "sequence"}
            for name in input_names
            if sample[name].dim() > 1
        }
        dynamic_axes["logits"] = {0: "batch"}
        # Tuple positionnel aligné sur input_names : la forward du modèle
        # accepte ces tensors dans l'ordre (input_ids, attention_mask,
        # éventuellement token_type_ids).
        sample_tuple = tuple(sample[name] for name in input_names)

        torch.onnx.export(
            model,
            sample_tuple,
            str(output_path),
            input_names=input_names,
            output_names=["logits"],
            dynamic_axes=dynamic_axes,
            opset_version=opset_version,
            do_constant_folding=True,
            # Exporter legacy (torchscript) : supporte dynamic_axes et reste
            # stable quel que soit l'exporter dynamo (défaut depuis torch 2.4,
            # qui exige onnxscript + dynamic_shapes).
            dynamo=False,
        )

    logger.info("Export ONNX terminé : %s (opset %d)", output_path, opset_version)
    return output_path


def softmax_logits(logits: np.ndarray) -> np.ndarray:
    """Softmax numérique stable sur le dernier axe (shape conservée)."""
    shifted = logits - np.max(logits, axis=-1, keepdims=True)
    exp = np.exp(shifted)
    return exp / exp.sum(axis=-1, keepdims=True)


class ONNXClassificationEngine:
    """Moteur d'inférence ONNX (classification) avec post-traitement standard.

    Charge paresseusement la session ``onnxruntime`` (premier ``predict``) et
    applique : tokenisation (max_length/truncation), normalisation des entrées,
    softmax sur les logits, argmax sur les ``labels`` fournis.

    Args:
        model_path: chemin du fichier ``.onnx`` exporté ;
        tokenizer: tokenizer HuggingFace associé au modèle ;
        labels: libellés des classes dans l'ordre de la tête de sortie ;
        max_length: longueur maximale de tokenisation ;
        providers: fournisseurs ONNX Runtime (défaut CPU).
    """

    def __init__(
        self,
        model_path: str | Path,
        tokenizer: Any,
        labels: list[str],
        *,
        max_length: int = 128,
        providers: list[str] | None = None,
    ) -> None:
        self.model_path = str(model_path)
        self._tokenizer = tokenizer
        self.labels = list(labels)
        self.max_length = max_length
        self.providers = providers or ["CPUExecutionProvider"]
        self._session: Any = None
        self._session_lock = threading.Lock()

    def _load_session(self) -> Any:
        """Session onnxruntime (chargée une seule fois, verrouillée)."""
        if self._session is None:
            try:
                import onnxruntime as ort
            except ImportError as exc:  # pragma: no cover - message clair
                raise RuntimeError(
                    "onnxruntime est requis pour l'inférence ONNX "
                    "(pip install onnxruntime)."
                ) from exc
            with self._session_lock:
                if self._session is None:
                    self._session = ort.InferenceSession(
                        self.model_path,
                        providers=self.providers,
                    )
        return self._session

    def _input_feed(self, texts: list[str]) -> dict[str, np.ndarray]:
        encoding = self._tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=self.max_length,
            return_tensors="np",
        )
        available = set(encoding)
        session = self._load_session()
        session_inputs = [node.name for node in session.get_inputs()]
        feed: dict[str, np.ndarray] = {}
        for name in session_inputs:
            if name in available:
                feed[name] = np.asarray(encoding[name], dtype=np.int64)
        if not feed:
            raise RuntimeError(
                f"Aucune entrée du modèle ONNX ({session_inputs}) "
                f"parmi celles du tokenizer ({sorted(available)})."
            )
        return feed

    def predict(self, texts: list[str]) -> list[dict[str, Any]]:
        """Prédit pour une liste de textes : listes de dicts standardisés."""
        if not texts:
            return []
        session = self._load_session()
        feed = self._input_feed(texts)
        outputs = session.run(None, feed)
        logits = np.asarray(outputs[0])
        probs = softmax_logits(logits)
        predictions = probs.argmax(axis=-1)

        results: list[dict[str, Any]] = []
        for text, prob_row, index in zip(texts, probs, predictions, strict=True):
            index = int(index)
            results.append(
                {
                    "text": text,
                    "label": self.labels[index],
                    "confidence": round(float(prob_row[index]), 3),
                }
            )
        return results
