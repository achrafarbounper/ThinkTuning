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
        self.classifier = torch.nn.Linear(3, 3)

    def forward(self, input_ids, attention_mask=None, **kwargs):
        h = self.proj(input_ids.to(torch.float32))
        logits = self.classifier(h)
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


def test_label_smoothing_epsilon_is_used_in_criterion():
    """Le label smoothing epsilon configurable est passé au CrossEntropyLoss."""
    trainer = Trainer(
        model=TinyTextModel(),
        cfg=_make_cfg(label_smoothing=0.2, mixup_alpha=0.0),
    )
    assert trainer.criterion.label_smoothing == 0.2
    assert torch.nn.CrossEntropyLoss().reduction == trainer.criterion.reduction


def test_label_smoothing_defaults_to_zero():
    """Par défaut (clé absente), aucun label smoothing n'est appliqué."""
    trainer = Trainer(model=TinyTextModel(), cfg=_make_cfg())
    assert trainer.criterion.label_smoothing == 0.0


def test_mixup_disabled_calls_criterion_once():
    """Mixup désactivé (alpha=0) : la loss du batch permuté n'est pas calculée."""
    model = TinyTextModel()
    model.proj.weight.data.zero_()
    model.proj.bias.data.zero_()

    trainer = Trainer(model=model, cfg=_make_cfg(mixup_alpha=0.0))
    trainer.scheduler = MagicMock()

    sample = {
        "input_ids": torch.tensor([[1, 2, 3, 4]], dtype=torch.long),
        "attention_mask": torch.ones((1, 4), dtype=torch.long),
        "labels": torch.tensor([1], dtype=torch.long),
    }
    loader = DataLoader([sample], batch_size=1)

    criterion = MagicMock(return_value=torch.tensor(1.0, requires_grad=True))
    trainer.criterion = criterion

    trainer._train_epoch(loader)

    criterion.assert_called_once()
    assert len(criterion.call_args.args) == 2
    assert trainer.mixup_alpha == 0.0


def test_mixup_enabled_combines_two_criterion_calls():
    """Mixup activé : la loss combine vrai batch + batch permuté (2 appels)."""
    model = TinyTextModel()
    model.proj.weight.data.zero_()
    model.proj.bias.data.zero_()

    # alpha=1.0 => Beta(1,1)=uniforme => lam ∈ [0.5, 1.0] après max(lam,1-lam)
    trainer = Trainer(model=model, cfg=_make_cfg(mixup_alpha=1.0))
    trainer.scheduler = MagicMock()
    assert trainer.mixup_alpha == 1.0

    samples = [
        {"input_ids": torch.tensor([[1, 2, 3, 4]], dtype=torch.long),
         "attention_mask": torch.ones((1, 4), dtype=torch.long),
         "labels": torch.tensor([i], dtype=torch.long)}
        for i in range(3)
    ]
    loader = DataLoader(samples, batch_size=3)
    batch = next(iter(loader))
    original_labels = batch["labels"]

    criterion = MagicMock(
        return_value=torch.tensor(0.5, requires_grad=True)
    )
    trainer.criterion = criterion

    with patch.dict("src.model.trainer.np.random.__dict__",
                    {"beta": lambda a, b: 0.7}):
        trainer._train_epoch(loader)

    # 2 appels : vrai batch + batch permuté
    assert criterion.call_count == 2
    first_logits, first_labels = criterion.call_args_list[0].args
    second_logits, second_labels = criterion.call_args_list[1].args
    assert torch.equal(first_logits, second_logits)
    # Le 2e appel reçoit la même batch de labels réordonnée (permutation)
    assert sorted(second_labels.tolist()) == sorted(original_labels.tolist())


class DummyTokenizer:
    def save_pretrained(self, path):
        os.makedirs(path, exist_ok=True)


class _HFStyleModel:
    """Imite un PreTrainedModel HF : save_pretrained écrit config.json + model.safetensors."""

    def save_pretrained(self, path):
        from safetensors.torch import save_file

        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "config.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        # Modèle minimal avec tête de classification entraînée (std > 0)
        # pour passer la validation _is_valid_model_dir de model_versioning.
        cls_weight = torch.randn(3, 768)
        cls_bias = torch.zeros(3)
        save_file(
            {"classifier.weight": cls_weight, "classifier.bias": cls_bias},
            os.path.join(path, "model.safetensors"),
        )


def test_save_model_version_writes_training_report(monkeypatch, tmp_path):
    # Redirige MODEL_ROOT vers un tmp : sans cela, chaque run pytest publie un
    # dossier de stub dans experiments/models/ (pollution de l'état réel, qui
    # cassait ensuite /predict et les tests résolvant la dernière version).
    from core import model_versioning as _mv

    monkeypatch.setattr(_mv, "MODEL_ROOT", str(tmp_path / "models"))
    monkeypatch.setattr(_mv, "MODELS_ROOT", str(tmp_path / "models"))

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
    with open(report_path, encoding="utf-8") as fp:
        payload = json.load(fp)

    assert payload["job_id"] == "job-123"
    assert payload["train_examples"] == 42
    assert payload["val_examples"] == 12
    assert payload["training_duration_seconds"] == 12.5
    assert "hyperparameters" in payload
    assert "metrics" in payload
    assert payload["metrics"]["f1_by_epoch"] == [0.8]
    assert payload["metrics"]["accuracy_by_epoch"] == [0.85]


def test_save_model_version_writes_config_and_safetensors_weights(monkeypatch, tmp_path):
    """Le dossier produit par POST /train doit être chargeable par /predict :
    il doit contenir config.json + model.safetensors pour un modèle HF."""
    from core import model_versioning as _mv

    monkeypatch.setattr(_mv, "MODEL_ROOT", str(tmp_path / "models"))
    monkeypatch.setattr(_mv, "MODELS_ROOT", str(tmp_path / "models"))

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

    def fake_eval_epoch(_loader, cancel_event=None, on_progress=None,
                        epoch=None, epochs_total=None):
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

    def fake_eval_epoch(_loader, cancel_event=None, on_progress=None,
                        epoch=None, epochs_total=None):
        return {"accuracy": 0.5, "f1_macro": next(f1_values)}

    trainer._eval_epoch = fake_eval_epoch

    with patch.object(Trainer, "save"):
        result = trainer.train(loader, loader)

    assert not trainer.early_stopped
    assert result["early_stopped"] is False
    assert len(trainer.epoch_metrics) == 3
    # Malgré tout, le meilleur checkpoint est restauré en fin de training
    assert trainer.final_metrics["epoch"] == 1
