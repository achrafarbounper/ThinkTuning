# project/tests/test_model_activation.py

"""Tests d'activation et de validation des versions de modele (SCRUM-55)."""

import json
import os

import pytest

from core import model_activation, model_versioning


@pytest.fixture()
def version_dir(tmp_path, monkeypatch):
    """Construit une version complete (poids + config + mappings) dans MODEL_ROOT temporaire."""
    import torch

    root = tmp_path / "models"
    vdir = root / "20260101T000000Z"
    vdir.mkdir(parents=True)
    (vdir / "config.json").write_text(
        json.dumps({"model_type": "test", "num_labels": 3}), encoding="utf-8"
    )
    (vdir / "id2label.json").write_text(
        json.dumps({"0": "negative", "1": "neutral", "2": "positive"}), encoding="utf-8"
    )
    (vdir / "label2id.json").write_text(
        json.dumps({"negative": 0, "neutral": 1, "positive": 2}), encoding="utf-8"
    )
    # Tete de classification entrainee : ecart-type > 0.03.
    torch.save({"classifier.weight": torch.randn(3, 8) * 0.5}, vdir / "model_state_dict.pt")
    monkeypatch.setattr(model_versioning, "MODEL_ROOT", str(root))
    monkeypatch.setattr(model_activation, "MODEL_ROOT", str(root))
    return vdir


@pytest.fixture()
def active_pointer(tmp_path, monkeypatch):
    pointer = str(tmp_path / "active.json")
    monkeypatch.setenv("ACTIVE_MODEL_POINTER", pointer)
    # Le module lit l'env a chaque appel via get_active_pointer_path().
    return pointer


def test_activate_model_rejects_unknown_version(active_pointer):
    with pytest.raises(ValueError):
        model_activation.activate_model("inconnue")


def test_activate_model_writes_pointer(version_dir, active_pointer):
    version = os.path.basename(str(version_dir))
    data = model_activation.activate_model(version)
    assert data["version"] == version
    assert os.path.isfile(active_pointer)
    assert model_activation.is_active(version)
    assert model_activation.get_active_model_dir() == os.path.abspath(str(version_dir))


def test_activate_model_rejects_untrained_head(tmp_path, monkeypatch, active_pointer):
    import torch

    root = tmp_path / "models"
    vdir = root / "20260101T000001Z"
    vdir.mkdir(parents=True)
    (vdir / "config.json").write_text("{}", encoding="utf-8")
    (vdir / "id2label.json").write_text("{}", encoding="utf-8")
    (vdir / "label2id.json").write_text("{}", encoding="utf-8")
    # Tete quasi aleatoire : std <= 0.03
    torch.save({"classifier.weight": torch.full((3, 8), 0.001)}, vdir / "model_state_dict.pt")
    monkeypatch.setattr(model_versioning, "MODEL_ROOT", str(root))
    monkeypatch.setattr(model_activation, "MODEL_ROOT", str(root))

    with pytest.raises(ValueError, match="non activable"):
        model_activation.activate_model("20260101T000001Z")


def test_validate_model_version_accepts_complete_version(version_dir):
    result = model_versioning.validate_model_version(str(version_dir))
    assert result["valid"] is True


def test_validate_model_version_rejects_incomplete(tmp_path, monkeypatch):
    root = tmp_path / "models"
    vdir = root / "20260101T000002Z"
    vdir.mkdir(parents=True)
    monkeypatch.setattr(model_versioning, "MODEL_ROOT", str(root))

    with pytest.raises(ValueError) as excinfo:
        model_versioning.validate_model_version(str(vdir))
    message = str(excinfo.value)
    assert "config.json" in message
    assert "poids" in message
    assert "id2label" in message


def test_validate_model_version_rejects_incoherent_mappings(version_dir):
    (version_dir / "label2id.json").write_text(
        json.dumps({"negative": 0, "neutral": 1}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="incoherents"):
        model_versioning.validate_model_version(str(version_dir))


def test_resolve_model_dir_prefers_active_version(version_dir, active_pointer, monkeypatch):
    # Deux versions : la plus recente doit etre ignoree si une version active existe.
    newer = version_dir.parent / "20260202T000000Z"
    newer.mkdir()
    (newer / "model_state_dict.pt").write_bytes(b"x")

    version = os.path.basename(str(version_dir))
    model_activation.activate_model(version)
    assert model_versioning.resolve_model_dir() == str(version_dir)
