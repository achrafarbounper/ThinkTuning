"""Tests Phase 3 : export ONNX et moteur ONNX Runtime.

Validation réelle (hors-ligne) : on construit un mini BertForSequenceClassification
(random), un tokenizer minimal, on exporte en ONNX via ``torch.onnx`` puis on
infère avec ``ONNXClassificationEngine``. Les dépendances ``onnx`` /
``onnxruntime`` sont désormais requises du projet (Phase 3) ; si elles
manquent, les tests sont ignorés proprement (``importorskip``).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import torch

pytest.importorskip("onnx")
pytest.importorskip("onnxruntime")

from core.onnx_exporter import (  # noqa: E402
    ONNXClassificationEngine,
    export_model_to_onnx,
    softmax_logits,
)

_TOKENS = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]", "bonjour", "monde", "super"]


def _tiny_tokenizer(root: Path):
    """Tokenizer minimal hors-ligne depuis un vocab.txt."""
    from transformers import BertTokenizer

    vocab_dir = root / "vocab"
    vocab_dir.mkdir(parents=True, exist_ok=True)
    (vocab_dir / "vocab.txt").write_text("\n".join(_TOKENS) + "\n", encoding="utf-8")
    return BertTokenizer.from_pretrained(str(vocab_dir))


def _tiny_model(tokenizer):
    """Mini modèle de classification (poids aléatoires, déterminisme torture fix)."""
    from transformers import BertConfig, BertForSequenceClassification

    config = BertConfig(
        vocab_size=len(_TOKENS),
        hidden_size=16,
        num_hidden_layers=1,
        num_attention_heads=2,
        intermediate_size=32,
        max_position_embeddings=32,
        num_labels=3,
    )
    torch.manual_seed(0)
    model = BertForSequenceClassification(config)
    model.eval()
    return model


@pytest.fixture(scope="module")
def onnx_fixture(tmp_path_factory):
    """Exporte le mini modèle une seule fois par session de test."""
    root = tmp_path_factory.mktemp("onnx")
    tokenizer = _tiny_tokenizer(root)
    model = _tiny_model(tokenizer)
    output_dir = root / "model"
    output_dir.mkdir(exist_ok=True)
    onnx_path = export_model_to_onnx(
        model,
        tokenizer,
        output_dir / "model.onnx",
        opset_version=14,
    )
    return tokenizer, onnx_path


class TestSoftmax:
    def test_distribution_valide(self) -> None:
        logits = np.array([[1.0, 2.0, 3.0], [0.5, -1.5, 0.0]])
        probs = softmax_logits(logits)
        assert probs.shape == logits.shape
        np.testing.assert_allclose(probs.sum(axis=-1), np.ones(2), atol=1e-6)
        assert np.all(probs >= 0.0)

    def test_stabilité_numerique(self) -> None:
        logits = np.array([[1000.0, 1000.0, 1000.0]])
        probs = softmax_logits(logits)
        np.testing.assert_allclose(probs[0], [1 / 3] * 3, atol=1e-3)


class TestExportOnnx:
    def test_fichier_cree(self, onnx_fixture) -> None:
        _, onnx_path = onnx_fixture
        assert onnx_path.is_file()
        assert onnx_path.stat().st_size > 0

    def test_reexport_ecrase(self, onnx_fixture, tmp_path) -> None:
        _, onnx_path = onnx_fixture
        tokenizer = _tiny_tokenizer(tmp_path)
        model = _tiny_model(tokenizer)
        target = tmp_path / "again" / "model.onnx"
        export_model_to_onnx(model, tokenizer, target)
        export_model_to_onnx(model, tokenizer, target)  # écrase sans erreur
        assert target.is_file()


class TestEngine:
    def test_predict_single(self, onnx_fixture) -> None:
        tokenizer, onnx_path = onnx_fixture
        engine = ONNXClassificationEngine(
            onnx_path,
            tokenizer,
            labels=["negative", "neutral", "positive"],
            max_length=16,
        )
        results = engine.predict(["bonjour monde"])
        assert len(results) == 1
        assert results[0]["text"] == "bonjour monde"
        assert results[0]["label"] in {"negative", "neutral", "positive"}
        assert 0.0 <= results[0]["confidence"] <= 1.0

    def test_predict_batch_ordre_preserve(self, onnx_fixture) -> None:
        tokenizer, onnx_path = onnx_fixture
        engine = ONNXClassificationEngine(
            onnx_path,
            tokenizer,
            labels=["negative", "neutral", "positive"],
            max_length=16,
        )
        texts = ["bonjour", "monde", "super bonjour monde"]
        results = engine.predict(texts)
        assert len(results) == 3
        assert [r["text"] for r in results] == texts

    def test_predict_empty(self, onnx_fixture) -> None:
        tokenizer, onnx_path = onnx_fixture
        engine = ONNXClassificationEngine(onnx_path, tokenizer, labels=["a", "b"])
        assert engine.predict([]) == []

    def test_session_paressese_et_reutilisee(self, onnx_fixture) -> None:
        tokenizer, onnx_path = onnx_fixture
        engine = ONNXClassificationEngine(onnx_path, tokenizer, labels=["a", "b", "c"])
        assert engine._session is None  # pas d'import tant qu'on ne prédit pas
        engine.predict(["un"])
        session_1 = engine._session
        engine.predict(["un"])
        assert engine._session is session_1  # session réutilisée
