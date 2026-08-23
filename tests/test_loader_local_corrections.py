"""
Tests du chargement des corrections locales dans le pipeline d'entraînement
(SCRUM-57) : load_local_corrections / load_raw_dataset(local_corrections_path=...),
rétrocompatibilité sans le paramètre, erreurs explicites sur fichier manquant
ou mal formé, et propagation depuis TrainRequest via core.trainer_runner.

Le hub Hugging Face est stubé (patch de src.dataset.loader.load_dataset) pour
que les tests restent offline et déterministes — même approche que
tests/test_preprocess.py qui patche load_raw_dataset côté runner.
"""

import json
import os
import tempfile
from unittest.mock import Mock, patch

import pytest
from datasets import ClassLabel, Dataset, Value

from src.dataset import loader as loader_module
from src.dataset.loader import (
    CORRECTIONS_REQUIRED_COLUMNS,
    load_local_corrections,
    load_raw_dataset,
)

CORRECTION_ROWS = [
    {"text": "Service client au top !", "label": 2, "lang_code": "fr"},
    {"text": "Mediocre quality overall.", "label": 0, "lang_code": "en"},
]


def _write_csv(path, rows):
    import csv

    with open(path, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CORRECTIONS_REQUIRED_COLUMNS))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path


def _fake_hub_dataset():
    """Mini dataset Hub avec label typé ClassLabel, comme le Parquet converti HF."""
    ds = Dataset.from_dict({
        "text": [f"tweet numero {i}" for i in range(6)],
        "label": [0, 1, 2, 0, 1, 2],
    })
    return ds.cast_column(
        "label", ClassLabel(names=["negative", "neutral", "positive"])
    )


@pytest.fixture
def patched_hub():
    """Patche load_dataset pour renvoyer un petit dataset HF hors-ligne."""
    with patch.object(loader_module, "load_dataset", return_value=_fake_hub_dataset()):
        yield


# ------------------------------------------------------------------ #
# Chargement du fichier de corrections                                #
# ------------------------------------------------------------------ #
def test_load_local_corrections_supports_csv_and_jsonl(tmp_path):
    csv_path = _write_csv(tmp_path / "corrections.csv", CORRECTION_ROWS)
    jsonl_path = _write_jsonl(tmp_path / "corrections.jsonl", CORRECTION_ROWS)

    for path in (csv_path, jsonl_path):
        ds = load_local_corrections(str(path))

        assert sorted(ds.column_names) == ["label", "lang_code", "text"]
        assert list(ds["text"]) == [row["text"] for row in CORRECTION_ROWS]
        assert list(ds["label"]) == [2, 0]
        assert list(ds["lang_code"]) == ["fr", "en"]
        assert ds.features["label"] == Value("int64")


def test_load_local_corrections_accepts_label_names_and_string_ints(tmp_path):
    rows = [
        {"text": "Superbe", "label": "positive", "lang_code": "FR"},
        {"text": "Bof", "label": "1", "lang_code": "fr"},
        {"text": "Nul", "label": 0, "lang_code": "en"},
    ]
    path = _write_jsonl(tmp_path / "mixed.jsonl", rows)

    ds = load_local_corrections(str(path))

    assert list(ds["label"]) == [2, 1, 0]


# ------------------------------------------------------------------ #
# Erreurs explicites : fichier manquant ou mal formé                  #
# ------------------------------------------------------------------ #
def test_missing_corrections_file_raises_filenotfound(tmp_path, patched_hub):
    missing = str(tmp_path / "does_not_exist.csv")

    with pytest.raises(FileNotFoundError, match="introuvable"):
        load_local_corrections(missing)

    # Le message reste explicite quand on passe par load_raw_dataset
    with pytest.raises(FileNotFoundError, match="introuvable"):
        load_raw_dataset(max_per_lang=3, local_corrections_path=missing)


def test_unsupported_extension_raises_explicit_error(tmp_path):
    path = tmp_path / "corrections.txt"
    path.write_text("text,label,lang_code\nBonjour,1,fr\n", encoding="utf-8")

    with pytest.raises(ValueError, match="CSV.*JSONL"):
        load_local_corrections(str(path))


def test_missing_columns_raise_explicit_error(tmp_path):
    path = tmp_path / "missing_lang.csv"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("text,label\nSuper produit,2\n")

    with pytest.raises(ValueError) as excinfo:
        load_local_corrections(str(path))

    message = str(excinfo.value)
    assert "lang_code" in message
    assert "text" in message and "label" in message  # colonnes attendues rappelées


def test_invalid_label_raises_explicit_error(tmp_path):
    rows = [{"text": "Bof", "label": "peut-être", "lang_code": "fr"}]
    path = _write_jsonl(tmp_path / "bad_label.jsonl", rows)

    with pytest.raises(ValueError, match="label invalide"):
        load_local_corrections(str(path))


def test_out_of_range_int_label_raises_explicit_error(tmp_path):
    rows = [{"text": "Bof", "label": 7, "lang_code": "fr"}]
    path = _write_csv(tmp_path / "bad_range.csv", rows)

    with pytest.raises(ValueError, match="label invalide"):
        load_local_corrections(str(path))


def test_unsupported_lang_code_raises_explicit_error(tmp_path):
    rows = [{"text": "Hola", "label": 2, "lang_code": "es"}]
    path = _write_csv(tmp_path / "bad_lang.csv", rows)

    with pytest.raises(ValueError) as excinfo:
        load_local_corrections(str(path))

    message = str(excinfo.value)
    assert "es" in message
    assert "fr" in message and "en" in message  # langues supportées rappelées


def test_empty_text_cell_raises_explicit_error(tmp_path):
    path = tmp_path / "empty_text.csv"
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("text,label,lang_code\n,2,fr\n")

    with pytest.raises(ValueError, match="'text' vide"):
        load_local_corrections(str(path))


def test_malformed_jsonl_line_raises_explicit_error(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text(
        '{"text": "ok", "label": 1, "lang_code": "fr"}\n{invalid json}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Ligne JSON invalide.*ligne 2"):
        load_local_corrections(str(path))


# ------------------------------------------------------------------ #
# Concaténation HF + corrections dans load_raw_dataset                #
# ------------------------------------------------------------------ #
def test_load_raw_dataset_without_param_unchanged(patched_hub):
    """Sans local_corrections_path : comportement historique strictement conservé."""
    merged = load_raw_dataset(max_per_lang=3)

    assert len(merged) == 6  # 3 par langue (fr, en), aucune correction
    assert set(merged["lang_code"]) == {"fr", "en"}
    # La branche par défaut ne touche pas au typage ClassLabel du hub
    assert isinstance(merged.features["label"], ClassLabel)


def test_load_raw_dataset_concatenates_local_corrections(tmp_path, patched_hub):
    csv_path = _write_csv(tmp_path / "corrections.csv", CORRECTION_ROWS)

    merged = load_raw_dataset(max_per_lang=3, local_corrections_path=str(csv_path))

    # 3 exemples/langue x 2 langues + 2 corrections locales
    assert len(merged) == 8

    texts = list(merged["text"])
    for row in CORRECTION_ROWS:
        assert row["text"] in texts

    # Les métadonnées des corrections sont préservées (lang_code + label)
    correction_indices = [texts.index(row["text"]) for row in CORRECTION_ROWS]
    lang_codes = list(merged["lang_code"])
    labels = list(merged["label"])
    for idx, row in zip(correction_indices, CORRECTION_ROWS):
        assert lang_codes[idx] == row["lang_code"]
        assert labels[idx] == row["label"]

    # Le typage ClassLabel du hub a été aligné sur int64 pour la concaténation
    assert merged.features["label"] == Value("int64")


def test_load_raw_dataset_with_empty_corrections_file_keeps_base(tmp_path, patched_hub):
    path = tmp_path / "empty.csv"
    path.write_text("text,label,lang_code\n", encoding="utf-8")

    merged = load_raw_dataset(max_per_lang=3, local_corrections_path=str(path))

    assert len(merged) == 6


# ------------------------------------------------------------------ #
# Propagation TrainRequest -> run_training -> load_raw_dataset        #
# ------------------------------------------------------------------ #
def test_run_training_propagates_local_corrections_to_loader():
    from core import trainer_runner as _runner
    import api
    from api import JobStatus, TrainJob, TrainRequest, _jobs

    raw = Dataset.from_dict({
        "text": ["Bonjour", "Hello", "Très bien", "Good"],
        "label": [0, 1, 2, 1],
        "lang_code": ["fr", "en", "fr", "en"],
    })
    job_id = "job-corrections"
    _jobs[job_id] = TrainJob(job_id=job_id, status=JobStatus.PENDING)

    cfg = {
        "model_name": "distilbert-base-multilingual-cased",
        "max_length": 16,
        "batch_size": 2,
        "num_workers": 0,
        "epochs": 1,
        "learning_rate": 1e-4,
        "weight_decay": 0.01,
        "warmup_ratio": 0.1,
        "device": "cpu",
    }
    fake_tokenizer = Mock(save_pretrained=Mock())

    mock_load_raw = Mock(return_value=raw)

    with patch.object(_runner, "load_config", return_value=cfg), \
         patch.object(_runner, "load_raw_dataset", mock_load_raw), \
         patch.object(_runner, "augment_dataset", side_effect=lambda ds, **kwargs: ds), \
         patch.object(_runner, "create_dataloaders", return_value=(Mock(), Mock())), \
         patch.object(_runner.AutoTokenizer, "from_pretrained", return_value=fake_tokenizer), \
         patch.object(_runner, "build_model", return_value=Mock()), \
         patch.object(_runner, "save_model_version", return_value="fake_model_dir"), \
         patch.object(_runner, "Trainer"), \
         patch.object(_runner, "TEST_MODE", False):
        _runner.run_training(job_id, TrainRequest(local_corrections_path="data/corrections.csv"))

    assert _jobs[job_id].status == JobStatus.COMPLETED
    mock_load_raw.assert_called_once()
    assert mock_load_raw.call_args.kwargs["local_corrections_path"] == "data/corrections.csv"
    assert mock_load_raw.call_args.kwargs["max_per_lang"] == TrainRequest().max_per_lang

