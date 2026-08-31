import logging
import os
from types import SimpleNamespace

import torch
from datasets import Dataset
from transformers import AutoTokenizer

from evaluate import evaluate, plot_confusion_matrices, report_by_language, segment_by_language


class DummyModel(torch.nn.Module):
    """Modèle déterministe qui prédit toujours la classe 0 (logits nuls)."""

    def forward(self, input_ids, attention_mask=None, **kwargs):
        batch_size = input_ids.size(0)
        return SimpleNamespace(logits=torch.zeros(batch_size, 3))


def _build_tokenized_with_lang():
    tokenizer = AutoTokenizer.from_pretrained("distilbert-base-multilingual-cased")
    dataset = Dataset.from_dict({
        "text": ["Bonjour", "Salut", "Hello", "Good", "Très bien", "Great"],
        "label": [0, 1, 2, 0, 1, 2],
        "lang_code": ["fr", "fr", "en", "en", "fr", "en"],
    })
    tokenized = dataset.map(
        lambda batch: tokenizer(batch["text"], truncation=True, max_length=8),
        batched=True,
    )
    if "label" in tokenized.column_names and "labels" not in tokenized.column_names:
        tokenized = tokenized.rename_column("label", "labels")
    return tokenized


def _run_evaluate():
    tokenizer = AutoTokenizer.from_pretrained("distilbert-base-multilingual-cased")
    tokenized = _build_tokenized_with_lang()
    return evaluate(DummyModel(), tokenizer, tokenized, batch_size=2)


def test_evaluate_segments_lang_code():
    results = _run_evaluate()

    # Rétro-compatibilité : métriques globales toujours au premier niveau.
    assert "accuracy" in results
    assert "f1_macro" in results

    # La colonne lang_code doit être conservée dans le résultat.
    assert results["langs"] == ["fr", "fr", "en", "en", "fr", "en"]
    assert results["labels"].shape == (6,)
    assert results["preds"].shape == (6,)


def test_segment_by_language_splits_fr_and_en():
    results = _run_evaluate()
    per_lang = segment_by_language(results)

    assert set(per_lang.keys()) == {"fr", "en"}
    assert per_lang["fr"]["labels"].size == 3
    assert per_lang["en"]["labels"].size == 3


def test_report_by_language_displays_confusion_matrices(capsys, caplog, tmp_path, monkeypatch):
    results = _run_evaluate()
    monkeypatch.setattr("evaluate.OUTPUT_DIR", str(tmp_path))

    with caplog.at_level(logging.INFO, logger="evaluate"):
        report_by_language(results)

    # Deux matrices distinctes affichées : une pour FR, une pour EN.
    messages = [r.getMessage() for r in caplog.records if r.name == "evaluate"]
    assert any("Français (FR)" in m for m in messages)
    assert any("Anglais (EN)" in m for m in messages)
    assert any("Matrice de confusion" in m for m in messages)

    # La figure ConfusionMatrixDisplay (FR + EN) est générée et sauvegardée.
    assert os.path.exists(os.path.join(str(tmp_path), "confusion_matrices_by_lang.png"))


def test_report_by_language_handles_missing_lang(caplog):
    tokenizer = AutoTokenizer.from_pretrained("distilbert-base-multilingual-cased")
    dataset = _build_tokenized_with_lang().remove_columns(["lang_code"])

    results = evaluate(DummyModel(), tokenizer, dataset, batch_size=2)
    assert results["langs"] is None

    with caplog.at_level(logging.WARNING, logger="evaluate"):
        report_by_language(results)

    assert any("pas de segmentation par langue" in r.getMessage() for r in caplog.records if r.name == "evaluate")
