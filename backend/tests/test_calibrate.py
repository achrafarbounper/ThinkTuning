"""
Tests du script de calibration (calibrate.py) :

    - courbe de calibration (reliability diagram) via sklearn.calibration_curve
    - ECE (Expected Calibration Error) + avertissement log au-delà de 0.1
    - temperature scaling post-hoc (application manuelle + ajustement NLL)
"""

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import torch
from datasets import Dataset
from transformers import AutoTokenizer

import calibrate
from calibrate import (
    analyze,
    apply_temperature_scaling,
    expected_calibration_error,
    fit_temperature,
    reliability_curve,
    warn_if_miscalibrated,
)

LOGGER_NAME = "calibrate"


# --------------------------------------------------------------------------- #
# Temperature scaling : application
# --------------------------------------------------------------------------- #

def test_apply_temperature_scaling_normalizes_and_keeps_argmax():
    rng = np.random.default_rng(42)
    logits = rng.normal(size=(64, 3)) * 3.0

    probs = apply_temperature_scaling(logits, 2.5)

    assert probs.shape == (64, 3)
    # Chaque ligne est une distribution de probabilité valide.
    assert np.allclose(probs.sum(axis=1), 1.0)
    assert (probs >= 0).all()
    # Le temperature scaling ne change jamais la classe prédite (monotonie).
    assert np.array_equal(probs.argmax(axis=1), logits.argmax(axis=1))


def test_higher_temperature_softens_confidence():
    rng = np.random.default_rng(0)
    logits = rng.normal(size=(128, 3)) * 5.0

    confident = apply_temperature_scaling(logits, 1.0).max(axis=1)
    softened = apply_temperature_scaling(logits, 5.0).max(axis=1)

    # T > 1 adoucit : chaque confiance individuelle diminue (jamais n'augmente).
    assert (softened <= confident + 1e-12).all()
    assert softened.mean() < confident.mean()


def test_apply_temperature_scaling_rejects_non_positive_temperature():
    with pytest.raises(ValueError):
        apply_temperature_scaling(np.array([[1.0, 2.0, 3.0]]), 0.0)
    with pytest.raises(ValueError):
        apply_temperature_scaling(np.array([[1.0, 2.0, 3.0]]), -1.0)


# --------------------------------------------------------------------------- #
# ECE + seuil d'avertissement
# --------------------------------------------------------------------------- #

def test_expected_calibration_error_zero_for_perfectly_calibrated_bins():
    # Bin 1 : confiance 0.25, précision réelle 0.25 ; bin 2 : confiance 0.75,
    # précision 0.75 -> gaps nuls -> ECE = 0.
    confidences = np.array([0.25] * 400 + [0.75] * 400)
    correct = np.array([1] * 100 + [0] * 300 + [1] * 300 + [0] * 100)

    ece, details = expected_calibration_error(confidences, correct, n_bins=10)

    assert ece < 1e-9
    assert len(details) == 2


def test_expected_calibration_error_overconfident_model():
    # Le modèle annonce 0.9 partout mais n'a raison qu'une fois sur deux :
    # gap = 0.4 sur tous les échantillons -> ECE = 0.4.
    confidences = np.full(100, 0.9)
    correct = np.tile([1.0, 0.0], 50)

    ece, details = expected_calibration_error(confidences, correct, n_bins=10)

    assert abs(ece - 0.4) < 1e-9
    assert len(details) == 1
    assert details[0]["count"] == 100
    assert abs(details[0]["mean_confidence"] - 0.9) < 1e-12
    assert abs(details[0]["accuracy"] - 0.5) < 1e-12


def test_expected_calibration_error_empty_input_returns_zero():
    ece, details = expected_calibration_error(np.array([]), np.array([]), n_bins=10)
    assert ece == 0.0
    assert details == []


def test_warn_if_miscalibrated_logs_warning_when_ece_above_0_1(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        warned = warn_if_miscalibrated(0.15)

    assert warned is True
    warnings = [r for r in caplog.records
                if r.name == LOGGER_NAME and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    # L'avertissement mentionne l'ECE mesuré et le seuil dépassé.
    assert "0.15" in warnings[0].getMessage()
    assert "0.1" in warnings[0].getMessage()


def test_no_warning_when_ece_below_threshold(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        warned = warn_if_miscalibrated(0.05)

    assert warned is False
    assert not [r for r in caplog.records
                if r.name == LOGGER_NAME and r.levelno == logging.WARNING]


def test_warning_threshold_is_0_1():
    # Critère d'acceptation : avertissement dès que l'ECE dépasse 0.1.
    assert calibrate.ECE_WARNING_THRESHOLD == 0.1


# --------------------------------------------------------------------------- #
# Courbe de calibration (sklearn.calibration.calibration_curve)
# --------------------------------------------------------------------------- #

def test_reliability_curve_uses_sklearn_and_returns_monotonic_points():
    rng = np.random.default_rng(11)
    n = 4000
    # Confiances uniformes ; précision réelle croissante avec la confiance.
    confidences = rng.uniform(0.05, 1.0, size=n)
    correct = (rng.random(n) < confidences).astype(float)

    prob_true, prob_pred = reliability_curve(confidences, correct, n_bins=10)

    assert prob_true.shape == prob_pred.shape
    assert prob_true.size >= 2
    assert ((prob_true >= 0) & (prob_true <= 1)).all()
    assert ((prob_pred >= 0) & (prob_pred <= 1)).all()
    # sklearn renvoie les bins triés par confiance croissante.
    assert (np.diff(prob_pred) > 0).all()


# --------------------------------------------------------------------------- #
# Ajustement de la température (minimisation NLL)
# --------------------------------------------------------------------------- #

def _sample_labels_from_softmax(logits, temperature, seed):
    """Tire des labels selon la loi softmax(logits / T) (inverse CDF vectorisé)."""
    probs = apply_temperature_scaling(logits, temperature)
    rng = np.random.default_rng(seed)
    u = rng.random(size=(logits.shape[0], 1))
    return (u > probs.cumsum(axis=1)).sum(axis=1)


def test_fit_temperature_recovers_known_temperature():
    rng = np.random.default_rng(3)
    base_logits = rng.normal(size=(4096, 3))
    true_temperature = 2.0

    # Labels générés par un modèle « trop froid » (T=2) : sur-confiant une fois
    # ramené à T=1. La température optimale doit retomber proche de 2.0.
    labels = _sample_labels_from_softmax(base_logits, true_temperature, seed=7)

    fitted = fit_temperature(base_logits, labels)

    assert 1.6 < fitted < 2.4

    # L'ajustement doit réduire la NLL par rapport à T=1.0.
    def nll(t):
        probs = apply_temperature_scaling(base_logits, t)
        return -np.log(probs[np.arange(len(labels)), labels] + 1e-12).mean()

    assert nll(fitted) < nll(1.0)


def test_fit_temperature_returns_one_for_degenerate_input(caplog):
    # Une seule classe présente dans les labels : pas de signal pour ajuster T.
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        temperature = fit_temperature(np.ones((4, 3)), np.array([0, 0, 0, 0]))

    assert temperature == 1.0


# --------------------------------------------------------------------------- #
# Analyse bout-en-bout sur logits synthétiques
# --------------------------------------------------------------------------- #

def _overconfident_logits(n=600, scale=10.0, seed=5):
    """Logits très tranchés mais prédictions aléatoires : sur-confiant extrême."""
    rng = np.random.default_rng(seed)
    preds = rng.integers(0, 3, size=n)
    logits = np.zeros((n, 3))
    logits[np.arange(n), preds] = scale
    labels = rng.integers(0, 3, size=n)  # indépendants -> précision ~ 1/3
    return logits, labels


def test_analyze_detects_overconfidence_then_temperature_scaling_fixes_it():
    logits, labels = _overconfident_logits()

    base = analyze(logits, labels, n_bins=10)

    # Modèle quasi certain de lui (~1.0) mais précis à ~1/3 : ECE >> 0.1.
    assert base["accuracy"] < 0.45
    assert base["mean_confidence"] > 0.95
    assert base["ece"] > 0.1

    # Un T élevé ramène la confiance vers ~1/3, soit proche de la précision.
    fixed = analyze(logits, labels, n_bins=10, temperature=60.0)

    assert fixed["accuracy"] == base["accuracy"]  # argmax invariant
    assert fixed["mean_confidence"] < 0.5
    assert fixed["ece"] < 0.1
    assert fixed["ece"] < base["ece"]


def test_analyze_bin_details_are_json_serializable():
    logits, labels = _overconfident_logits(n=120)

    result = analyze(logits, labels, n_bins=5)

    payload = json.dumps({"bins": result["bins"], "ece": result["ece"],
                          "temperature": result["temperature"]})
    assert "bins" in payload


# --------------------------------------------------------------------------- #
# Intégration : collecte des logits sur un vrai tokenizer (comme evaluate.py)
# --------------------------------------------------------------------------- #

def test_collect_logits_and_full_pipeline_with_dummy_model():
    class ConstantModel(torch.nn.Module):
        """Renvoie toujours un logit massif pour la classe 0 : sur-confiant."""

        def forward(self, input_ids, attention_mask=None, **kwargs):
            batch_size = input_ids.size(0)
            logits = torch.tensor([[8.0, 0.1, 0.1]]).repeat(batch_size, 1)
            return SimpleNamespace(logits=logits)

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

    logits, labels = calibrate.collect_logits(ConstantModel(), tokenizer,
                                              tokenized, batch_size=2)

    assert logits.shape == (4, 3)
    assert labels.shape == (4,)
    assert np.array_equal(labels, np.array([0, 1, 2, 0]))

    # Pipeline complet : analyse + détection du mauvais calibrage.
    result = analyze(logits, labels, n_bins=10)

    # Toutes les prédictions sont la classe 0 avec ~1.0 de confiance ->
    # précision 0.5, confiance moyenne ~1.0 -> ECE ~0.5 > 0.1.
    assert result["accuracy"] == 0.5
    assert result["mean_confidence"] > 0.99
    assert result["ece"] > 0.1
    assert warn_if_miscalibrated(result["ece"]) is True



def test_plot_reliability_diagram_saves_png(tmp_path):
    prob_true = np.array([0.2, 0.5, 0.9])
    prob_pred = np.array([0.3, 0.55, 0.85])
    save_path = tmp_path / "curve.png"

    result = calibrate.plot_reliability_diagram(
        prob_true, prob_pred, ece=0.08, save_path=str(save_path),
        temperature=1.7,
    )

    assert result == str(save_path)
    assert save_path.exists()
    assert save_path.stat().st_size > 0


# --------------------------------------------------------------------------- #
# Sélection du modèle versionné (experiments/models)
# --------------------------------------------------------------------------- #

def _make_version(models_root, name, valid=True):
    """Crée une fausse version de modèle (poids factices si valide)."""
    path = models_root / name
    path.mkdir(parents=True, exist_ok=True)
    if valid:
        Path(path, "model.safetensors").write_bytes(b"weights")
    return path


def test_select_model_defaults_to_latest_valid_version(tmp_path, monkeypatch):
    root = tmp_path / "experiments" / "models"
    _make_version(root, "20260101T000000Z")
    # Version plus récente mais SANS poids -> invalide, doit être ignorée.
    _make_version(root, "20260303T000000Z", valid=False)
    _make_version(root, "20260202T000000Z")
    monkeypatch.chdir(tmp_path)

    path, label = calibrate.select_model_path()

    assert label == "20260202T000000Z"  # dernière VALIDE (la 03 est ignorée)
    assert Path(path).is_dir()
    assert Path(path).name == label


def test_select_model_explicit_valid_name(tmp_path, monkeypatch):
    root = tmp_path / "experiments" / "models"
    _make_version(root, "20260101T000000Z")
    _make_version(root, "20260202T000000Z")
    monkeypatch.chdir(tmp_path)

    path, label = calibrate.select_model_path("20260101T000000Z")

    assert label == "20260101T000000Z"
    assert Path(path).name == label


def test_select_model_rejects_invalid_name_and_lists_available(tmp_path, monkeypatch):
    _make_version(tmp_path / "experiments" / "models", "20260101T000000Z")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(ValueError) as excinfo:
        calibrate.select_model_path("inconnu")

    # Le message d'erreur liste les versions disponibles pour aider au choix.
    assert "20260101T000000Z" in str(excinfo.value)


def test_select_model_raises_when_no_valid_model_exists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # experiments/models absent -> aucune version

    with pytest.raises(FileNotFoundError):
        calibrate.select_model_path()


