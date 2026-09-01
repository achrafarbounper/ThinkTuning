# project/tests/test_annotation_store.py

"""Tests du store d'annotations du cycle Active Learning (SCRUM-55)."""

import json
import os

import pytest

from core.annotation_store import (
    AnnotationStore,
    normalize_label,
    normalize_text,
)


@pytest.fixture()
def store(tmp_path):
    return AnnotationStore(str(tmp_path / "annotations.jsonl"))


def test_normalize_label_accepts_names_and_aliases():
    assert normalize_label("negative") == 0
    assert normalize_label("neutral") == 1
    assert normalize_label("positive") == 2
    assert normalize_label("negatif") == 0
    assert normalize_label("Positif") == 2
    assert normalize_label("0") == 0
    assert normalize_label("2") == 2


def test_normalize_label_rejects_invalid():
    with pytest.raises(ValueError):
        normalize_label("joy")
    with pytest.raises(ValueError):
        normalize_label("")
    with pytest.raises(ValueError):
        normalize_label(7)


def test_annotate_deduplicates_by_normalized_text(store):
    store.annotate("Service terrible", "negative")
    store.annotate("service terrible", "positif")  # meme texte, label maj
    assert store.count() == 1
    records = store.list()
    assert records[0]["label"] == 2  # derniere annotation gagne


def test_annotate_rejects_empty_text_and_bad_label(store):
    with pytest.raises(ValueError):
        store.annotate("   ", "positive")
    with pytest.raises(ValueError):
        store.annotate("ok", "inconnu")


def test_persistence_across_instances(tmp_path):
    path = str(tmp_path / "journal.jsonl")
    first = AnnotationStore(path)
    first.annotate("Texte de test", "neutral")
    second = AnnotationStore(path)
    assert second.count() == 1
    assert second.list()[0]["text"] == "Texte de test"


def test_journal_file_is_jsonl(store):
    store.annotate("abc", "positive")
    with open(store.path, "r", encoding="utf-8") as fh:
        lines = [line for line in fh if line.strip()]
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["text"] == "abc"
    assert rec["label"] == 2


def test_export_review_csv(store, tmp_path):
    store.annotate("très mauvais", "negatif")
    out = store.export_review_csv(str(tmp_path / "review.csv"))
    assert os.path.isfile(out)
    import csv

    with open(out, "r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 1
    assert rows[0]["manual_label"] == "negative"
    assert rows[0]["status"] == "reviewed"


def test_remove(store):
    store.annotate("a supprimer", "neutral")
    assert store.remove("A SUPPRIMER ") is True
    assert store.count() == 0
    assert store.remove("inexistant") is False


def test_normalize_text_folds_case():
    assert normalize_text("  Hello World ") == "hello world"
