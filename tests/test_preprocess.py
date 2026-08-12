import unittest

import torch
from datasets import Dataset

from src.dataset.preprocess import tokenize_dataset, create_dataloaders


class TestPreprocess(unittest.TestCase):
    def test_tokenize_dataset_returns_tokenized_dataset(self):
        data = {"text": ["Bonjour le monde!", "Hello world!"], "label": [0, 2]}
        dataset = Dataset.from_dict(data)

        tokenized = tokenize_dataset(dataset, "distilbert-base-multilingual-cased", max_length=16)

        self.assertIn("input_ids", tokenized.column_names)
        self.assertIn("attention_mask", tokenized.column_names)
        self.assertEqual(len(tokenized), 2)

    def test_create_dataloaders_returns_valid_loaders(self):
        data = {"text": ["Bonjour le monde!", "Hello world!", "Coucou !"], "label": [0, 1, 2]}
        dataset = Dataset.from_dict(data)
        cfg = {"model_name": "distilbert-base-multilingual-cased", "batch_size": 2, "device": "cpu", "num_workers": 0}

        train_loader, val_loader = create_dataloaders(dataset, cfg)

        self.assertEqual(len(train_loader.dataset) + len(val_loader.dataset), 3)
        batch = next(iter(train_loader))
        self.assertTrue(torch.is_tensor(batch["input_ids"]))
        self.assertTrue(torch.is_tensor(batch["attention_mask"]))
        self.assertTrue(torch.is_tensor(batch["label"]))
