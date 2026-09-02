import csv
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

from merge_reviewed_data import (
    extract_valid_corrections,
    load_review_csv,
    main,
    merge_corrections_into_source,
    write_merged_csv,
    write_merged_jsonl,
)
from src.dataset.loader import LABEL_NAMES, load_local_dataset


class TestExtractValidCorrections(unittest.TestCase):
    def test_rows_without_manual_label_are_excluded(self):
        rows = [
            {"text": "Avis tranché", "manual_label": "positive"},
            {"text": "Sans avis", "manual_label": ""},
            {"text": "Valeur absente"},
            {"text": "Espaces seulement", "manual_label": "   "},
            {"text": "Label inconnu", "manual_label": "unknown"},
            {"text": "Alias français", "manual_label": "Positif"},
        ]
        corrections = extract_valid_corrections(rows)
        # Seules les lignes tranchées sont conservées.
        self.assertEqual([c["text"] for c in corrections], ["Avis tranché", "Alias français"])

    def test_label_mapping_is_consistent_with_label_names(self):
        rows = [
            {"text": "n", "manual_label": "negative"},
            {"text": "u", "manual_label": "neutral"},
            {"text": "p", "manual_label": "positive"},
        ]
        corrections = extract_valid_corrections(rows)
        self.assertEqual([c["label"] for c in corrections], [0, 1, 2])
        # Cohérence avec LABEL_NAMES de src/dataset/loader.py.
        for correction in corrections:
            self.assertEqual(LABEL_NAMES[correction["label"]], {
                0: "negative", 1: "neutral", 2: "positive",
            }[correction["label"]])
        self.assertEqual(LABEL_NAMES[0], "negative")
        self.assertEqual(LABEL_NAMES[1], "neutral")
        self.assertEqual(LABEL_NAMES[2], "positive")

    def test_duplicates_within_review_counted_once(self):
        rows = [
            {"text": "Super produit", "manual_label": "positive"},
            {"text": "  super produit ", "manual_label": "negative"},  # doublon normalisé
            {"text": "Autre texte", "manual_label": "neutral"},
        ]
        corrections = extract_valid_corrections(rows)
        self.assertEqual(len(corrections), 2)
        # Première occurrence gagnante.
        self.assertEqual(corrections[0]["text"], "Super produit")
        self.assertEqual(corrections[0]["label"], 2)


class TestMergeCorrectionsIntoSource(unittest.TestCase):
    def test_duplicates_are_not_reinjected_twice(self):
        source = [
            {"text": "Très déçu", "label": 0, "lang_code": "fr"},
            {"text": "Très déçu", "label": 0, "lang_code": "fr"},  # doublon interne source
            {"text": "Produit solide", "label": 2, "lang_code": "en"},
        ]
        corrections = [
            {"text": "très déçu", "label": 1},       # déjà présent -> mise à jour du label
            {"text": "  TRÈS DÉÇU  ", "label": 0},   # doublon normalisé -> ignoré
            {"text": "Nouvelle phrase", "label": 2}, # absent de la source -> ajout
        ]
        merged, stats = merge_corrections_into_source(corrections, source, default_lang_code="fr")

        normalized = [r["text"].strip().lower() for r in merged]
        self.assertEqual(
            len(normalized),
            len(set(normalized)),
            "chaque texte normalisé doit apparaître une seule fois",
        )
        self.assertEqual(len(merged), 3)

        updated = next(r for r in merged if r["text"].strip().lower() == "très déçu")
        self.assertEqual(updated["label"], 1)  # la correction manuelle prime
        self.assertEqual(stats["corrected_existing"], 1)
        self.assertEqual(stats["appended_new"], 1)
        self.assertEqual(stats["source_duplicates_removed"], 1)
        self.assertEqual(stats["duplicate_corrections_skipped"], 1)
        self.assertEqual(stats["final_rows"], 3)

        appended = merged[-1]
        self.assertEqual(appended["text"], "Nouvelle phrase")
        self.assertEqual(appended["label"], 2)
        self.assertEqual(appended["lang_code"], "fr")  # langue par défaut pour les ajouts


class TestWriteOutputs(unittest.TestCase):
    def _sample_records(self):
        return [
            {"text": "Bonjour", "label": 2, "lang_code": "fr"},
            {"text": "Hello", "label": 0, "lang_code": "en"},
        ]

    def test_write_merged_jsonl_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "train_enriched.jsonl")
            write_merged_jsonl(self._sample_records(), out_path)
            with open(out_path, encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle if line.strip()]
        self.assertEqual(rows, self._sample_records())

    def test_write_merged_csv_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = os.path.join(tmpdir, "train_enriched.csv")
            write_merged_csv(self._sample_records(), out_path)
            with open(out_path, encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames, ["text", "label", "lang_code"])
                rows = [{**row, "label": int(row["label"])} for row in reader]
        self.assertEqual(rows, self._sample_records())


class TestLoadLocalDataset(unittest.TestCase):
    def test_load_alpaca_jsonl_and_plain_csv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Format Alpaca (input/output) : celui de data/train.jsonl.
            jsonl_path = os.path.join(tmpdir, "source.jsonl")
            with open(jsonl_path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"instruction": "x", "input": "Service impeccable", "output": "neutral"}) + "\n")
                handle.write(json.dumps({"instruction": "x", "input": "Très mauvais", "output": "negative"}) + "\n")

            dataset = load_local_dataset(jsonl_path)
            self.assertEqual(dataset["text"], ["Service impeccable", "Très mauvais"])
            self.assertEqual(dataset["label"], [1, 0])
            self.assertEqual(dataset["lang_code"], ["fr", "fr"])  # langue par défaut

            # Format natif text/label/lang_code : sortie de merge_reviewed_data.py.
            csv_path = os.path.join(tmpdir, "source.csv")
            with open(csv_path, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["text", "label", "lang_code"])
                writer.writeheader()
                writer.writerow({"text": "Great product", "label": "2", "lang_code": "en"})

            dataset = load_local_dataset(csv_path)
            self.assertEqual(dataset["text"], ["Great product"])
            self.assertEqual(dataset["label"], [2])
            self.assertEqual(dataset["lang_code"], ["en"])

    def test_invalid_label_raises_value_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "bad.csv")
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write("text,label\n")
                handle.write("Texte quelconque,peu importe\n")
            with self.assertRaises(ValueError):
                load_local_dataset(path)


class TestCliEndToEnd(unittest.TestCase):
    def test_main_merges_review_into_training_set(self):
        original_argv = sys.argv[:]
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tmpdir:
            source_path = os.path.join(tmpdir, "train.jsonl")
            with open(source_path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"input": "Service impeccable", "output": "neutral"}) + "\n")
                handle.write(json.dumps({"input": "Qualité moyenne", "output": "negative"}) + "\n")

            review_path = os.path.join(tmpdir, "manual_review_template.csv")
            with open(review_path, "w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["text", "predicted_label", "manual_label", "status"])
                writer.writeheader()
                writer.writerow({
                    "text": "Service impeccable",
                    "predicted_label": "neutral",
                    "manual_label": "negative",
                    "status": "ok",
                })
                writer.writerow({
                    "text": "Marque nouvelle ligne",
                    "predicted_label": "",
                    "manual_label": "positive",
                    "status": "ok",
                })
                writer.writerow({
                    "text": "Non tranchée",
                    "predicted_label": "positive",
                    "manual_label": "",
                    "status": "",
                })

            output_path = os.path.join(tmpdir, "train_enriched.jsonl")
            try:
                sys.argv = [
                    "merge_reviewed_data.py",
                    "--review", review_path,
                    "--source", source_path,
                    "--output", output_path,
                    "--format", "jsonl",
                ]
                with redirect_stdout(stdout):
                    main()
            finally:
                sys.argv = original_argv

            with open(output_path, encoding="utf-8") as handle:
                rows = [json.loads(line) for line in handle if line.strip()]

        # Ligne non tranchée exclue ; correction appliquée ; nouvel exemple ajouté.
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], {"text": "Service impeccable", "label": 0, "lang_code": "fr"})
        self.assertEqual(rows[1], {"text": "Qualité moyenne", "label": 0, "lang_code": "fr"})
        self.assertEqual(rows[2], {"text": "Marque nouvelle ligne", "label": 2, "lang_code": "fr"})
        self.assertIn("Exported 3 examples", stdout.getvalue())

    def test_load_review_csv_normalizes_and_tolerates_bom(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "review.csv")
            with open(path, "w", encoding="utf-8-sig", newline="") as handle:
                handle.write("text,predicted_label,manual_label,status\n")
                handle.write('"Texte avec BOM",Positive,Négatif,ok\n')

            rows = load_review_csv(path)

        self.assertEqual(rows[0]["manual_label"], "negative")  # alias français normalisé
        self.assertEqual(rows[0]["predicted_label"], "positive")


if __name__ == "__main__":
    unittest.main()
