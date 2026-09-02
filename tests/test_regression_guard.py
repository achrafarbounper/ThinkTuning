"""
Garde-fou anti-régression du continual training (core/trainer_runner.py).

Couvre :
- check_regression : comparaison F1 new vs source (seuil, None-safe) ;
- load_source_f1 : lecture tolérante du training_report.json d'une version.
"""

import json

import pytest

from core import trainer_runner

# --------------------------------------------------------------------------- #
# check_regression
# --------------------------------------------------------------------------- #

def test_check_regression_flags_when_f1_lower():
    detail = trainer_runner.check_regression(0.72, 0.80)
    assert detail is not None
    assert "0.7200" in detail and "0.8000" in detail


def test_check_regression_ok_when_f1_improves():
    assert trainer_runner.check_regression(0.85, 0.80) is None


def test_check_regression_threshold_tolerance():
    # Sous le seuil (0.01) : pas considéré comme régression.
    assert trainer_runner.check_regression(0.795, 0.80, threshold=0.01) is None
    # Au-delà du seuil : régression.
    assert trainer_runner.check_regression(0.78, 0.80, threshold=0.01) is not None


def test_check_regression_none_f1_is_never_a_regression():
    assert trainer_runner.check_regression(None, 0.80) is None
    assert trainer_runner.check_regression(0.72, None) is None
    assert trainer_runner.check_regression(None, None) is None


# --------------------------------------------------------------------------- #
# load_source_f1
# --------------------------------------------------------------------------- #

def _make_version(monkeypatch, tmp_path, version, report):
    root = tmp_path / "models"
    version_dir = root / version
    version_dir.mkdir(parents=True, exist_ok=True)
    if report is not None:
        (version_dir / "training_report.json").write_text(
            json.dumps(report), encoding="utf-8"
        )
    monkeypatch.setattr(trainer_runner, "MODEL_ROOT", str(root))
    return version_dir


def test_load_source_f1_reads_report(monkeypatch, tmp_path):
    _make_version(
        monkeypatch,
        tmp_path,
        "20260819T151459Z",
        {"metrics": {"accuracy": 0.85, "f1_macro": 0.80}},
    )
    assert trainer_runner.load_source_f1("20260819T151459Z") == pytest.approx(0.80)


def test_load_source_f1_missing_report_returns_none(monkeypatch, tmp_path):
    _make_version(monkeypatch, tmp_path, "20260819T151459Z", None)
    assert trainer_runner.load_source_f1("20260819T151459Z") is None


def test_load_source_f1_missing_version_returns_none(monkeypatch, tmp_path):
    _make_version(monkeypatch, tmp_path, "other", {"metrics": {"f1_macro": 0.9}})
    assert trainer_runner.load_source_f1("inconnu") is None


def test_load_source_f1_corrupted_report_returns_none(monkeypatch, tmp_path):
    version_dir = _make_version(monkeypatch, tmp_path, "broken", None)
    (version_dir / "training_report.json").write_text("{not json", encoding="utf-8")
    assert trainer_runner.load_source_f1("broken") is None

