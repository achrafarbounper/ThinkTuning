"""
Persistance fidèle du modèle entraîné (SCRUM : « tête de classification non
entraînée persistée dans le modèle sauvegardé »).

Couvre :
- Trainer.save() : les poids sur disque sont strictement identiques aux
  poids du modèle en mémoire ;
- _save_trained_model() : une tête légitimement fine-tunée dont le std reste
  ≈ 0.02 est bien publiée (pas de faux positif « quasi-aléatoire ») ;
- _save_trained_model() : une divergence entre les poids persistés et le
  modèle en mémoire est rejetée (réinstanciation détectée) ;
- head_matches_reference : équivalence stricte tête disque <-> mémoire.
"""

import os
from types import SimpleNamespace

import pytest
import torch
from torch.utils.data import DataLoader

from core import model_versioning
from core.model_head_check import head_matches_reference, is_model_version_trained
from core.model_versioning import save_model_version
from src.model.trainer import Trainer

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

class TinyTextModel(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.proj = torch.nn.Linear(4, 3)
        self.classifier = torch.nn.Linear(3, 3)

    def forward(self, input_ids, attention_mask=None, **kwargs):
        h = self.proj(input_ids.to(torch.float32))
        return SimpleNamespace(logits=self.classifier(h))


def _make_cfg(epochs=1, **overrides):
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


class DictDataset(torch.utils.data.Dataset):
    def __init__(self, n=6):
        torch.manual_seed(0)
        self.ids = torch.randint(1, 10, (n, 4))
        self.mask = torch.ones((n, 4), dtype=torch.long)
        self.labels = torch.tensor([0, 1, 2, 0, 1, 2][:n])

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, i):
        return {
            "input_ids": self.ids[i],
            "attention_mask": self.mask[i],
            "labels": self.labels[i],
        }


class _HFStyleModel:
    """Modèle « HF-like » : save_pretrained écrit config.json + safetensors.

    ``diverge=True`` simule une réinstanciation : save_pretrained écrit des
    poids de tête DIFFÉRENTS de ceux du state_dict en mémoire.
    """

    def __init__(self, diverge=False):
        torch.manual_seed(42)
        self._weight = torch.randn(3, 768) * 0.02  # std ≈ 0.02 (fine-tuning court)
        self._bias = torch.zeros(3)
        self._diverge = diverge

    def state_dict(self):
        return {
            "classifier.weight": self._weight.clone(),
            "classifier.bias": self._bias.clone(),
        }

    def save_pretrained(self, path):
        from safetensors.torch import save_file

        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "config.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        weight = self._weight + 1.0 if self._diverge else self._weight
        save_file(
            {
                "classifier.weight": weight.clone(),
                "classifier.bias": self._bias.clone(),
            },
            os.path.join(path, "model.safetensors"),
        )


class DummyTokenizer:
    def save_pretrained(self, path):
        os.makedirs(path, exist_ok=True)


def _trainer_from(model, with_metrics=True):
    """Stub de trainer (même interface que Trainer pour _save_trained_model)."""
    return SimpleNamespace(
        model=model,
        cfg={"model_name": "stub"},
        epoch_metrics=[{"epoch": 1, "accuracy": 0.6, "f1_macro": 0.55}] if with_metrics else [],
        final_metrics={"accuracy": 0.6, "f1_macro": 0.55} if with_metrics else {},
        training_duration_seconds=1.0,
    )


# --------------------------------------------------------------------------- #
# Trainer.save : fidélité mémoire -> disque
# --------------------------------------------------------------------------- #

def test_trainer_save_persists_exact_in_memory_weights(tmp_path, monkeypatch):
    """Le modèle persisté par Trainer.save() est strictement identique au
    modèle entraîné en mémoire (y compris la tête de classification)."""
    # Le checkpoint "best_model" de Trainer.train est écrit relativement au
    # CWD : on isole le test dans tmp_path.
    monkeypatch.chdir(tmp_path)
    torch.manual_seed(1)
    model = TinyTextModel()
    trainer = Trainer(model=model, cfg=_make_cfg())

    loader = DataLoader(DictDataset(), batch_size=2)
    trainer.train(loader, loader)

    out = tmp_path / "saved"
    trainer.save(str(out))

    from safetensors.torch import load_file

    if (out / "model.safetensors").exists():  # modèle avec save_pretrained
        saved = load_file(str(out / "model.safetensors"))
    else:  # repli state_dict torch pur
        saved = torch.load(out / "model_state_dict.pt", map_location="cpu")

    for name, ref in model.state_dict().items():
        assert name in saved, f"paramètre '{name}' absent du modèle sauvegardé"
        assert torch.equal(saved[name], ref.detach().cpu()), (
            f"le paramètre '{name}' diverge entre le modèle en mémoire et le "
            "modèle sauvegardé"
        )


# --------------------------------------------------------------------------- #
# _save_trained_model : faux positif std ≈ 0.02 (fine-tuning court)
# --------------------------------------------------------------------------- #

def test_save_model_version_publishes_short_finetuned_head(monkeypatch, tmp_path):
    """Une tête fine-tunée dont le std reste ≈ 0.02 (petit dataset) est
    publiée : l'attestation d'entraînement compense l'heuristique std."""
    monkeypatch.setattr(model_versioning, "MODEL_ROOT", str(tmp_path / "models"))

    model = _HFStyleModel()  # tête std ≈ 0.02 < seuil 0.03
    trainer = _trainer_from(model)

    model_dir = save_model_version(
        DummyTokenizer(), trainer, "job-short", 10, 4, 0.0, 1.0
    )

    assert os.path.isfile(os.path.join(model_dir, "model.safetensors"))
    assert os.path.isfile(os.path.join(model_dir, "training_report.json"))
    assert is_model_version_trained(model_dir)
    # Le dossier publié ne doit pas contenir de temporaire résiduel.
    assert not os.path.isdir(model_dir + ".tmp")
    # Les poids publiés correspondent au modèle en mémoire.
    from safetensors.torch import load_file

    saved = load_file(os.path.join(model_dir, "model.safetensors"))
    ref = model.state_dict()
    assert torch.equal(saved["classifier.weight"], ref["classifier.weight"])
    assert torch.equal(saved["classifier.bias"], ref["classifier.bias"])


# --------------------------------------------------------------------------- #
# _save_trained_model : détection de divergence mémoire <-> disque
# --------------------------------------------------------------------------- #

def test_save_model_version_rejects_diverged_weights(monkeypatch, tmp_path):
    """Si les poids persistés diffèrent du modèle en mémoire (réinstanciation
    de la tête), la sauvegarde échoue et rien n'est publié."""
    monkeypatch.setattr(model_versioning, "MODEL_ROOT", str(tmp_path / "models"))

    model = _HFStyleModel(diverge=True)
    trainer = _trainer_from(model)

    with pytest.raises(RuntimeError, match="DIFFÈRE"):
        save_model_version(
            DummyTokenizer(), trainer, "job-diverged", 10, 4, 0.0, 1.0
        )

    # Rien ne doit avoir été publié (le .tmp est nettoyé, pas de dossier final).
    published = tmp_path / "models"
    published_dirs = (
        [d for d in published.iterdir() if d.is_dir()] if published.exists() else []
    )
    assert published_dirs == []


def test_save_model_version_without_metrics_still_uses_std_heuristic(monkeypatch, tmp_path):
    """Sans métriques, une tête à std élevé reste valide (heuristique std)."""
    monkeypatch.setattr(model_versioning, "MODEL_ROOT", str(tmp_path / "models"))

    model = _HFStyleModel()
    model._weight = torch.randn(3, 768)  # std ≈ 0.58 > 0.03
    trainer = _trainer_from(model, with_metrics=False)
    trainer.epoch_metrics = []
    trainer.final_metrics = {}

    model_dir = save_model_version(
        DummyTokenizer(), trainer, "job-std", 10, 4, 0.0, 1.0
    )
    assert os.path.isfile(os.path.join(model_dir, "model.safetensors"))


# --------------------------------------------------------------------------- #
# head_matches_reference : unitaire
# --------------------------------------------------------------------------- #

def test_head_matches_reference_true_for_identical(tmp_path):
    from safetensors.torch import save_file

    w = torch.randn(3, 4)
    save_file({"classifier.weight": w}, str(tmp_path / "model.safetensors"))
    assert head_matches_reference(str(tmp_path), {"classifier.weight": w.clone()})


def test_head_matches_reference_false_for_divergence(tmp_path):
    from safetensors.torch import save_file

    save_file(
        {"classifier.weight": torch.randn(3, 4)},
        str(tmp_path / "model.safetensors"),
    )
    assert not head_matches_reference(
        str(tmp_path), {"classifier.weight": torch.randn(3, 4)}
    )


def test_head_matches_reference_false_for_missing_key(tmp_path):
    from safetensors.torch import save_file

    save_file(
        {"classifier.weight": torch.randn(3, 4)},
        str(tmp_path / "model.safetensors"),
    )
    # La clé de référence n'existe pas côté disque -> pas de correspondance.
    assert not head_matches_reference(str(tmp_path), {"autre.weight": torch.randn(1)})
    # Aucun state_dict de référence -> non vérifiable.
    assert not head_matches_reference(str(tmp_path), None)

    def __len__(self):
        return len(self.labels)

    def __getitem__(self, i):
        return {
            "input_ids": self.ids[i],
            "attention_mask": self.mask[i],
            "labels": self.labels[i],
        }
