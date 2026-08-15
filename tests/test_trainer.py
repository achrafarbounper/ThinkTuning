import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import torch
from torch.utils.data import DataLoader

from src.model.trainer import Trainer


class TinyTextModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.proj = torch.nn.Linear(4, 3)

    def forward(self, input_ids, attention_mask=None, **kwargs):
        logits = self.proj(input_ids.to(torch.float32))
        return SimpleNamespace(logits=logits)


class TestTrainer(unittest.TestCase):
    def test_train_epoch_uses_criterion_with_logits_and_labels(self):
        model = TinyTextModel()
        model.proj.weight.data.zero_()
        model.proj.bias.data.zero_()

        cfg = {
            "device": "cpu",
            "learning_rate": 1e-3,
            "weight_decay": 0.0,
            "epochs": 1,
            "warmup_ratio": 0.0,
            "gradient_accumulation_steps": 1,
            "gradient_clip": 1.0,
        }
        trainer = Trainer(model=model, cfg=cfg)
        trainer.scheduler = MagicMock()

        sample = {
            "input_ids": torch.tensor([[1, 2, 3, 4]], dtype=torch.long),
            "attention_mask": torch.ones((1, 4), dtype=torch.long),
            "labels": torch.tensor([1], dtype=torch.long),
        }
        loader = DataLoader([sample], batch_size=1)
        batch = next(iter(loader))
        expected_logits = model(
            input_ids=batch["input_ids"],
            attention_mask=batch["attention_mask"],
        ).logits

        criterion = MagicMock(return_value=torch.tensor(1.0, requires_grad=True))
        trainer.criterion = criterion

        trainer._train_epoch(loader)

        criterion.assert_called_once()
        self.assertEqual(len(criterion.call_args.args), 2)
        called_logits, called_labels = criterion.call_args.args
        self.assertEqual(called_logits.shape, expected_logits.shape)
        self.assertTrue(torch.equal(called_labels, batch["labels"]))
        trainer.scheduler.step.assert_called_once()
