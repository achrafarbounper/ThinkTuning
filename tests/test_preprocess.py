import threading
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch

import torch
from datasets import Dataset
from transformers import AutoTokenizer

from api import JobStatus, TrainJob, TrainRequest, _jobs, _job_cancel_events, _run_training, cancel_training
from evaluate import evaluate
from src.dataset.preprocess import tokenize_dataset, create_dataloaders


class TestPreprocess(unittest.TestCase):
    def test_tokenize_dataset_returns_tokenized_dataset(self):
        data = {"text": ["Bonjour le monde!", "Hello world!"], "label": [0, 2]}
        dataset = Dataset.from_dict(data)

        tokenized = tokenize_dataset(dataset, "distilbert-base-multilingual-cased", max_length=16)

        self.assertIn("input_ids", tokenized.column_names)
        self.assertIn("attention_mask", tokenized.column_names)
        self.assertIn("labels", tokenized.column_names)
        self.assertEqual(len(tokenized), 2)

    def test_create_dataloaders_returns_valid_loaders(self):
        data = {"text": ["Bonjour le monde!", "Hello world!", "Coucou !"], "label": [0, 1, 2]}
        dataset = Dataset.from_dict(data)
        split = dataset.train_test_split(test_size=1/3, seed=42)
        train_ds, val_ds = split["train"], split["test"]
        cfg = {"model_name": "distilbert-base-multilingual-cased", "batch_size": 2, "device": "cpu", "num_workers": 0}

        train_loader, val_loader = create_dataloaders(train_ds, val_ds, cfg)

        self.assertEqual(len(train_loader.dataset) + len(val_loader.dataset), 3)
        batch = next(iter(train_loader))
        self.assertTrue(torch.is_tensor(batch["input_ids"]))
        self.assertTrue(torch.is_tensor(batch["attention_mask"]))
        self.assertTrue(torch.is_tensor(batch["labels"]))

    def test_run_training_splits_dataset_before_creating_loaders(self):
        raw = Dataset.from_dict({
            "text": ["Bonjour", "Hello", "Très bien", "Good"],
            "label": [0, 1, 2, 1],
            "lang_code": ["fr", "en", "fr", "en"],
        })
        job_id = "job-split"
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

        with patch("api.load_config", return_value=cfg), \
             patch("api.load_raw_dataset", return_value=raw), \
             patch("api.augment_dataset", side_effect=lambda ds, **kwargs: ds), \
             patch("api.create_dataloaders", return_value=(MagicMock(), MagicMock())) as mock_create_dataloaders, \
             patch("api.AutoTokenizer.from_pretrained", return_value=MagicMock(save_pretrained=MagicMock())), \
             patch("api.build_model", return_value=MagicMock()), \
             patch("api.Trainer") as mock_trainer_cls:
            mock_trainer = MagicMock()
            mock_trainer_cls.return_value = mock_trainer

            _run_training(job_id, TrainRequest())

        self.assertEqual(_jobs[job_id].status, JobStatus.COMPLETED)
        self.assertEqual(mock_create_dataloaders.call_count, 1)
        self.assertEqual(len(mock_create_dataloaders.call_args.args), 3)
        self.assertEqual(mock_create_dataloaders.call_args.args[0].__class__.__name__, "Dataset")
        self.assertEqual(mock_create_dataloaders.call_args.args[1].__class__.__name__, "Dataset")
        self.assertEqual(mock_create_dataloaders.call_args.args[2]["device"], "cpu")
        mock_trainer.train.assert_called_once()
        mock_trainer.save.assert_called_once_with("./sentiment_model_final")

    def test_cancel_training_marks_job_cancelled_and_sets_event(self):
        job_id = "job-cancel"
        _jobs[job_id] = TrainJob(job_id=job_id, status=JobStatus.RUNNING)
        _job_cancel_events[job_id] = threading.Event()

        job = cancel_training(job_id)

        self.assertEqual(job.status, JobStatus.CANCELLED)
        self.assertTrue(_job_cancel_events[job_id].is_set())

    def test_evaluate_pads_variable_length_sequences(self):
        class DummyModel(torch.nn.Module):
            def forward(self, input_ids, attention_mask=None, **kwargs):
                batch_size = input_ids.size(0)
                return SimpleNamespace(logits=torch.randn(batch_size, 3))

        tokenizer = AutoTokenizer.from_pretrained("distilbert-base-multilingual-cased")
        dataset = Dataset.from_dict({
            "text": [
                "Bonjour le monde",
                "Salut",
                "Très bon produit, je recommande",
                "Mauvais achat",
            ],
            "label": [0, 1, 2, 0],
        })

        tokenized = dataset.map(
            lambda batch: tokenizer(batch["text"], truncation=True, max_length=8),
            batched=True,
        )
        if "label" in tokenized.column_names and "labels" not in tokenized.column_names:
            tokenized = tokenized.rename_column("label", "labels")

        metrics = evaluate(DummyModel(), tokenizer, tokenized, batch_size=2)

        self.assertIn("accuracy", metrics)
        self.assertIn("f1_macro", metrics)
