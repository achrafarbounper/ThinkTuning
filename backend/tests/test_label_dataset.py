import json
import logging
import os
import sys
import tempfile
import types

# Stub du module feuille src.inference.predictor AVANT d'importer
# label_dataset, exactement comme dans tests/test_active_learning.py, pour
# éviter de charger un vrai modèle DistilBERT pendant les tests.
#
# IMPORTANT : on n'injecte QUE le nom feuille (« src.inference.predictor »),
# jamais de faux modules parents (« src », « src.inference »). Un module nu
# créé via types.ModuleType() n'a pas de __path__ : s'il se retrouve dans
# sys.modules sous le nom « src », tout import ultérieur d'un sous-module
# (ex. api/__init__.py -> from src.utils.flags import TEST_MODE) échoue avec
# « No module named 'src.utils'; 'src' is not a package » pour le reste de
# la session pytest. setdefault garantit en plus qu'on n'écrase jamais un
# vrai module déjà importé par un autre test.
predictor_stub = types.ModuleType("src.inference.predictor")


class DummyPredictor:
    def __init__(self, *args, **kwargs):
        pass

    def predict(self, batch):
        return [{"text": text, "sentiment": "positive", "confidence": 0.9} for text in batch]


predictor_stub.Predictor = DummyPredictor
sys.modules.setdefault("src.inference.predictor", predictor_stub)

from label_dataset import build_alpaca_record, load_texts_from_file, main


def test_load_texts_from_csv():
    with tempfile.TemporaryDirectory() as tmpdir:
        csv_path = os.path.join(tmpdir, "texts.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("text,meta\n")
            f.write('"J\'adore ce produit !",A\n')
            f.write('"C\'est correct.",B\n')

        rows = load_texts_from_file(csv_path, text_column="text")
        assert rows == ["J'adore ce produit !", "C'est correct."]


def test_build_alpaca_record():
    record = build_alpaca_record(
        text="Super produit !",
        sentiment="positive",
        confidence=0.92,
    )

    assert record["instruction"] == "Classify the sentiment of the following text as negative, neutral, or positive."
    assert record["input"] == "Super produit !"
    assert record["output"] == "positive"
    assert record["confidence"] == 0.92


def test_load_texts_from_jsonl():
    with tempfile.TemporaryDirectory() as tmpdir:
        jsonl_path = os.path.join(tmpdir, "texts.jsonl")
        with open(jsonl_path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"text": "Tout va bien"}) + "\n")
            f.write(json.dumps({"text": "Très mauvais"}) + "\n")

        rows = load_texts_from_file(jsonl_path, text_column="text")
        assert rows == ["Tout va bien", "Très mauvais"]


def test_cli_accepts_min_confidence_flag(monkeypatch, tmp_path, caplog):
    """Le CLI accepte `--min_confidence` : test hermétique.

    Deux pièges corrigés :
    - `resolve_model_path(None)` exige un modèle entraîné dans
      experiments/models (absent en CI, dossier gitignore) -> on court-circuite
      la résolution ;
    - le stub sys.modules ci-dessus ne suffit pas en suite complète :
      api/__init__.py importe déjà le VRAI src.inference.predictor, donc on
      patche Predictor dans le namespace du module label_dataset.
    Le message « Exported » passe par logging (stderr), pas par stdout : on
    l'assertion via caplog, et on écrit l'entrée/sortie dans tmp_path.
    """
    input_path = tmp_path / "in.jsonl"
    input_path.write_text(
        json.dumps({"text": "Tout va bien"})
        + "\n"
        + json.dumps({"text": "Très mauvais"})
        + "\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "tmp_out.jsonl"

    monkeypatch.setattr(
        "label_dataset.resolve_model_path", lambda model_arg=None: "dummy-model-dir"
    )
    monkeypatch.setattr("label_dataset.Predictor", DummyPredictor)

    original_argv = sys.argv[:]
    try:
        sys.argv = [
            "label_dataset.py",
            "--input",
            str(input_path),
            "--output",
            str(output_path),
            "--min_confidence",
            "0.85",
        ]
        with caplog.at_level(logging.INFO, logger="label_dataset"):
            main()
    finally:
        sys.argv = original_argv

    assert "Exported" in caplog.text
    records = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
    ]
    # Confiance fictive 0.9 >= seuil 0.85 : les deux textes sont exportés.
    assert len(records) == 2
    assert all(record["confidence"] == 0.9 for record in records)
