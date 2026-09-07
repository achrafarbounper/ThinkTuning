import os
import uuid

from src.utils.config import load_config


def test_load_config_coerces_numeric_strings():
    # Le config loader refuse les chemins hors du projet (anti path traversal) :
    # on écrit donc le YAML de test dans tests/, sous un nom unique.
    tmp_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        f"_tmp_numeric_config_{uuid.uuid4().hex}.yaml",
    )
    try:
        with open(tmp_path, "w", encoding="utf-8") as tmp:
            tmp.write("max_length: '128'\nlearning_rate: '0.0005'\n")

        cfg = load_config(tmp_path)
        assert cfg["max_length"] == 128
        assert cfg["learning_rate"] == 0.0005
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_load_config_parses_class_augment_weights():
    """La config par défaut expose class_augment_weights avec neutral (1) surpondérée."""
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "configs", "default.yaml"
    )
    cfg = load_config(config_path)

    weights = cfg["class_augment_weights"]
    assert isinstance(weights, dict)
    assert set(weights) == {0, 1, 2}
    assert weights[1] > weights[0]
    assert weights[1] > weights[2]
