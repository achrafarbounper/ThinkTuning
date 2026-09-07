"""Classifieur d'intention (MiniLM, labels chat/action) — Phase 4.

Distingue un message « chat » (petite conversation, question générale) d'un
message « action » (demande d'exécution d'un outil : entraîner, chercher,
lister…). L'architecture :

  - modèle HuggingFace ``AutoModelForSequenceClassification`` chargé depuis une
    version valide de ``experiments/intent_models`` (voir ``core/intent_store``) ;
  - point de bascule : si AUCUN modèle entraîné n'est disponible (ou si son
    chargement échoue), les RÈGLES métier de ``ia/agent/classifiers/fallback``
    prennent le relais — continuité de service garantie ;
  - une variante ONNX est optionnelle ("onnx" → on réutilise
    ``ONNXClassificationEngine`` qui exige un fichier exporté).

Le classifieur ne dépend d'aucun import lourd au niveau du module : torch et
transformers ne sont chargés que lors de ``load_model``.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from core.intent_store import default_intent_labels, resolve_intent_model_dir
from ia.agent.classifiers.base import BaseClassifier, ClassifierMetrics, PredictionResult
from ia.agent.classifiers.fallback import fallback_intent

logger = logging.getLogger("thinktuning.agent.classifiers.intent")

_INTENT_LABELS = default_intent_labels()
_MODEL_MISSING_WARNED = False


def resolve_intent_model_optional(model_name: str | None = None) -> str | None:
    """Résout un modèle d'intention ou retourne None (pas de RuntimeError).

    Échec (aucune version valide) → ``None`` : le classifieur bascule alors
    sur les règles. N'émet le warning qu'une fois (évite le spam au runtime).
    """
    global _MODEL_MISSING_WARNED
    try:
        return resolve_intent_model_dir(model_name)
    except RuntimeError:
        if not _MODEL_MISSING_WARNED:
            logger.warning(
                "Aucun modèle d'intention entraîné : repli sur les règles "
                "métier (chat/action). Entraînez-le avec scripts/train_intent.py."
            )
            _MODEL_MISSING_WARNED = True
        return None


class IntentClassifier(BaseClassifier):
    """Classifieur chat/action avec repli automatique sur les règles.

    Args:
        model_name: nom de version spécifique (None = pointeur actif ou
            dernière version) ;
        engine: ``"auto"`` (défaut : PyTorch si disponible, sinon règles),
            ``"rules"`` (force les règles) ou ``"onnx"`` (exige un .onnx) ;
        quantize_int8: applique la quantification dynamique INT8 (torch) au
            chargement — réduit l'empreinte mémoire (~4x) sur CPU ;
        threshold: seuil de confiance minimal pour TRANCHER. En dessous, le
            résultat est ``"chat"`` par défaut (plus sûr de ne pas exécuter).
    """

    name = "intent"

    def __init__(
        self,
        model_name: str | None = None,
        *,
        engine: str = "auto",
        quantize_int8: bool = False,
        threshold: float = 0.5,
    ) -> None:
        if engine not in ("auto", "rules", "onnx"):
            raise ValueError(f"Moteur inconnu : {engine!r} (attendu : auto, rules, onnx)")
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("threshold doit être dans [0, 1]")
        self.model_name = model_name
        self.engine = engine
        self.quantize_int8 = quantize_int8
        self.threshold = threshold

        self._model = None          # PyTorch chargé (paresseux)
        self._tokenizer = None
        self._onnx = None
        self._metrics = ClassifierMetrics()
        self._load_error: str | None = None

    # -- Chargement -------------------------------------------------------

    def load_model(self) -> None:
        """Charge le moteur actif (idempotent). Bascule sur règles si échec."""
        if self._model is not None or self._onnx is not None:
            return
        if self.engine == "rules":
            return
        path = resolve_intent_model_optional(self.model_name)
        if path is None:
            self._load_error = "modèle indisponible -> règles"
            return
        try:
            if self.engine == "onnx":
                from transformers import AutoTokenizer

                from core.onnx_exporter import ONNXClassificationEngine

                onnx_path = self._find_onnx(path)
                self._onnx = ONNXClassificationEngine(
                    onnx_path,
                    AutoTokenizer.from_pretrained(path),
                    labels=list(_INTENT_LABELS),
                )
            else:
                import torch
                from transformers import (
                    AutoModelForSequenceClassification,
                    AutoTokenizer,
                )

                self._tokenizer = AutoTokenizer.from_pretrained(path)
                self._model = AutoModelForSequenceClassification.from_pretrained(
                    path, num_labels=len(_INTENT_LABELS)
                )
                if self.quantize_int8:
                    self._model = torch.quantization.quantize_dynamic(
                        self._model, dtype=torch.qint8
                    )
                    logger.info(
                        "Intention : quantification dynamique INT8 appliquée (%s)",
                        path,
                    )
                self._model.eval()
            logger.info(
                "IntentClassifier : modèle chargé (%s, engine=%s)", path, self.engine
            )
        except Exception as exc:
            self._load_error = f"échec chargement ({exc}) -> règles"
            logger.warning("IntentClassifier : %s", self._load_error)

    @property
    def _use_rules(self) -> bool:
        return (
            self.engine == "rules" or self._model is None and self._onnx is None
        )

    @staticmethod
    def _find_onnx(model_dir: str) -> str:
        """Chemin d'un .onnx sous ``model_dir`` (à la racine du dossier)."""
        candidate = os.path.join(model_dir, "model.onnx")
        if not os.path.isfile(candidate):
            raise FileNotFoundError(f"Aucun model.onnx dans {model_dir}")
        return candidate

    # -- Interface ---------------------------------------------------------

    def reload(self) -> None:
        """Oublie le modèle chargé et repart sur le disque / règles."""
        self._model = None
        self._tokenizer = None
        self._onnx = None
        self._load_error = None
        self.load_model()

    def predict(self, texts: list[str]) -> list[PredictionResult]:
        """Prédit pour une liste de textes (labels ``chat`` / ``action``)."""
        if not texts:
            return []
        self.load_model()

        if self._model is not None:
            return self._predict_torch(texts)
        if self._onnx is not None:
            return self._predict_onnx(texts)
        return self._predict_rules(texts)

    def _predict_rules(self, texts: list[str]) -> list[PredictionResult]:
        results: list[PredictionResult] = []
        for text in texts:
            label, confidence = fallback_intent(text)
            distribution = {
                label: float(confidence),
                next(lab for lab in _INTENT_LABELS if lab != label): round(
                    1.0 - confidence, 3
                ),
            }
            results.append(
                self._to_result(text, label, confidence, probabilities=distribution)
            )
        return results

    def _predict_torch(self, texts: list[str]) -> list[PredictionResult]:
        import torch

        start = time.perf_counter()
        inputs = self._tokenizer(
            texts, padding=True, truncation=True, max_length=128,
            return_tensors="pt",
        )
        with torch.inference_mode():
            logits = self._model(**inputs).logits
        probs = torch.softmax(logits, dim=-1)
        preds = probs.argmax(dim=-1)
        latency = (time.perf_counter() - start) * 1000.0
        results = []
        for i, (text, pred) in enumerate(zip(texts, preds, strict=True)):
            # Distribution complète des classes : rend visible la confiance
            # réelle du modèle (dont les prédictions proches de 50/50).
            distribution = {
                label: float(prob)
                for label, prob in zip(_INTENT_LABELS, probs[i].tolist(), strict=True)
            }
            label = _INTENT_LABELS[int(pred)]
            conf = float(probs[i, pred])
            results.append(
                self._to_result(text, label, conf, latency, probabilities=distribution)
            )
        self._metrics.record(latency, cached=False)
        return results

    def _predict_onnx(self, texts: list[str]) -> list[PredictionResult]:
        results = []
        for row in self._onnx.predict(texts):
            results.append(
                self._to_result(
                    row["text"],
                    row["label"],
                    row["confidence"],
                    probabilities=row.get("probabilities"),
                )
            )
        return results

    def _to_result(
        self,
        text: str,
        label: str,
        confidence: float,
        latency: float = 0.0,
        probabilities: dict[str, float] | None = None,
    ) -> PredictionResult:
        # Seuil : sous ``threshold``, on retombe sur ``chat`` (sécurité : ne
        # jamais déclencher une action sur une prédiction peu sûre). La
        # distribution reflète alors la DÉCISION rendue : l'invariant
        # ``probabilities[label] == confidence`` reste vrai après bascule.
        if confidence < self.threshold and label == "action":
            label, confidence = "chat", round(1.0 - confidence, 3)
            if probabilities is not None:
                other = next(lab for lab in _INTENT_LABELS if lab != label)
                probabilities = {
                    label: float(confidence),
                    other: round(1.0 - confidence, 3),
                }
        return PredictionResult(
            text=text, label=label, confidence=confidence, latency_ms=latency,
            model_name=self.model_name or "",
            probabilities=probabilities,
        )

    def get_model_info(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "engine": "rules" if self._use_rules else self.engine,
            "labels": list(_INTENT_LABELS),
            "threshold": self.threshold,
            "model_name": self.model_name or "active",
            "load_error": self._load_error,
        }

    def health_check(self) -> dict[str, Any]:
        try:
            result = self.predict(["Peux-tu lancer l'entraînement du modèle ?"])
            ok = result[0].label in _INTENT_LABELS
            return {
                "ok": ok,
                "label": result[0].label,
                "confidence": result[0].confidence,
            }
        except Exception as exc:  # pragma: no cover - chemin défensif
            return {"ok": False, "detail": str(exc)}

    def get_metrics(self) -> dict[str, Any]:
        return {"name": self.name, **self._metrics.to_dict()}
        self._load_error: str | None = None
