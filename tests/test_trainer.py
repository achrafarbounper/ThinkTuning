import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import torch
from torch.utils.data import DataLoader

import api
from src.model.trainer import Trainer


class TinyTextModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.proj = torch.nn.Linear(4, 3)

    def forward(self, input_ids, attention_mask=None, **kwargs):
        logits = self.proj(input_ids.to(torch.float32))
        return SimpleNamespace(logits=logits)


def _make_cfg(epochs=1, **overrides):
    """Config d'entraînement minimale pour les tests CPU offline."""
    cfg = {
        "device": "cpu",
        "learning_rate": 1e-3,
        "weight_decay": 0.0,
        "epochs": epochs,
        "warmup_ratio": 0.0,
        "gradient_accumulation_steps": 1,
        "gradient_clip": 1.0,
    }
    cfg.update(overrides)
    return cfg


def test_train_epoch_uses_criterion_with_logits_and_labels():
    model = TinyTextModel()
    model.proj.weight.data.zero_()
    model.proj.bias.data.zero_()

    trainer = Trainer(model=model, cfg=_make_cfg())
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
    assert len(criterion.call_args.args) == 2
    called_logits, called_labels = criterion.call_args.args
    assert called_logits.shape == expected_logits.shape
    assert torch.equal(called_labels, batch["labels"])
    trainer.scheduler.step.assert_called_once()


class DummyTokenizer:
    def save_pretrained(self, path):
        os.makedirs(path, exist_ok=True)


class _HFStyleModel:
    """Imite un PreTrainedModel HF : save_pretrained écrit config.json + model.safetensors."""

    def save_pretrained(self, path):
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "config.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        with open(os.path.join(path, "model.safetensors"), "w", encoding="utf-8") as fh:
            fh.write("")


def test_save_model_version_writes_training_report():
    model = TinyTextModel()
    cfg = _make_cfg(model_name="distilbert-base-uncased")
    trainer = Trainer(model=model, cfg=cfg)
    trainer.epoch_metrics = [
        {"epoch": 1, "accuracy": 0.85, "f1_macro": 0.8},
    ]
    trainer.final_metrics = {"accuracy": 0.85, "f1_macro": 0.8}
    trainer.training_duration_seconds = 12.5

    model_dir = api.save_model_version(
        DummyTokenizer(),
        trainer,
        job_id="job-123",
        train_examples=42,
        val_examples=12,
        started_at=1000.0,
        finished_at=1012.5,
    )

    report_path = os.path.join(model_dir, "training_report.json")
    assert os.path.exists(report_path)
    with open(report_path, "r", encoding="utf-8") as fp:
        payload = json.load(fp)

    assert payload["job_id"] == "job-123"
    assert payload["train_examples"] == 42
    assert payload["val_examples"] == 12
    assert payload["training_duration_seconds"] == 12.5
    assert "hyperparameters" in payload
    assert "metrics" in payload
    assert payload["metrics"]["f1_by_epoch"] == [0.8]
    assert payload["metrics"]["accuracy_by_epoch"] == [0.85]


def test_save_model_version_writes_config_and_safetensors_weights():
    """Le dossier produit par POST /train doit être chargeable par /predict :
    il doit contenir config.json + model.safetensors pour un modèle HF."""
    trainer = SimpleNamespace(
        model=_HFStyleModel(),
        cfg={"model_name": "distilbert-base-multilingual-cased"},
        epoch_metrics=[{"epoch": 1, "accuracy": 0.9, "f1_macro": 0.88}],
        final_metrics={"accuracy": 0.9, "f1_macro": 0.88},
        training_duration_seconds=1.0,
    )

    model_dir = api.save_model_version(
        DummyTokenizer(),
        trainer,
        job_id="job-hf",
        train_examples=10,
        val_examples=4,
        started_at=0.0,
        finished_at=1.0,
    )

    assert os.path.isfile(os.path.join(model_dir, "config.json"))
    assert os.path.isfile(os.path.join(model_dir, "model.safetensors"))


def test_early_stopping_stops_and_restores_best_checkpoint():
    model = TinyTextModel()
    cfg = _make_cfg(
        epochs=6,
        early_stopping_patience=3,
        early_stopping_min_delta=0.0,
    )
    trainer = Trainer(model=model, cfg=cfg)

    sample = {
        "input_ids": torch.tensor([1, 2, 3, 4], dtype=torch.long),
        "attention_mask": torch.ones(4, dtype=torch.long),
        "labels": torch.tensor(1, dtype=torch.long),
    }
    loader = DataLoader([sample], batch_size=1)

    # F1 de validation en dégradation : seule l'epoch 1 « améliore » (0.5)
    f1_values = iter([0.5, 0.4, 0.3, 0.2, 0.1, 0.0])

    def fake_eval_epoch(_loader, cancel_event=None):
        return {"accuracy": 0.5, "f1_macro": next(f1_values)}

    trainer._eval_epoch = fake_eval_epoch

    # L'enregistrement du checkpoint capture un cliché des poids "best"
    saved_snapshot = {}

    def fake_save(self, path):
        saved_snapshot["state"] = {
            k: v.detach().clone() for k, v in self.model.state_dict().items()
        }
        saved_snapshot["path"] = path

    with patch.object(Trainer, "save", new=fake_save):
        result = trainer.train(loader, loader)

    # 1 amélioration + patience=3 epochs sans progression -> arrêt à l'epoch 4
    assert trainer.early_stopped
    assert result["early_stopped"] is True
    assert len(trainer.epoch_metrics) == 4
    assert trainer.best_epoch == 1
    assert abs(trainer.best_f1 - 0.5) < 1e-6

    # Le meilleur checkpoint est sauvé sur disque…
    assert saved_snapshot["path"] == "experiments/checkpoints/best_model.pt"

    # …et restauré : final_metrics = l'epoch du meilleur modèle (pas la dernière)
    assert trainer.final_metrics["f1_macro"] == 0.5
    assert trainer.final_metrics["epoch"] == 1

    # Le modèle en mémoire contient les poids du meilleur checkpoint
    for name, tensor in saved_snapshot["state"].items():
        assert torch.equal(
            trainer.model.state_dict()[name], tensor
        ), f"le paramètre '{name}' n'a pas été restauré"


def test_early_stopping_disabled_by_default_runs_all_epochs():
    trainer = Trainer(model=TinyTextModel(), cfg=_make_cfg(epochs=3))

    sample = {
        "input_ids": torch.tensor([1, 2, 3, 4], dtype=torch.long),
        "attention_mask": torch.ones(4, dtype=torch.long),
        "labels": torch.tensor(1, dtype=torch.long),
    }
    loader = DataLoader([sample], batch_size=1)

    # F1 constant : sans patience configurée, l'entraînement va au bout
    f1_values = iter([0.5, 0.5, 0.5])

    def fake_eval_epoch(_loader, cancel_event=None):
        return {"accuracy": 0.5, "f1_macro": next(f1_values)}

    trainer._eval_epoch = fake_eval_epoch

    with patch.object(Trainer, "save"):
        result = trainer.train(loader, loader)

    assert not trainer.early_stopped
    assert result["early_stopped"] is False
    assert len(trainer.epoch_metrics) == 3
    # Malgré tout, le meilleur checkpoint est restauré en fin de training
    assert trainer.final_metrics["epoch"] == 1