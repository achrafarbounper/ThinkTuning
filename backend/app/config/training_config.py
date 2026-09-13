import yaml
import os

def _coerce_config(cfg):
    """Normalize config values that may be loaded as strings from YAML."""
    if not isinstance(cfg, dict):
        return cfg

    int_keys = {
        "max_length",
        "epochs",
        "batch_size",
        "num_workers",
        "early_stopping_patience",
    }
    float_keys = {
        "learning_rate",
        "weight_decay",
        "warmup_ratio",
        "early_stopping_min_delta",
        "label_smoothing",
        "mixup_alpha",
    }

    for key in int_keys:
        if key in cfg and not isinstance(cfg[key], int):
            try:
                cfg[key] = int(cfg[key])
            except (TypeError, ValueError):
                pass

    for key in float_keys:
        if key in cfg and not isinstance(cfg[key], float):
            try:
                cfg[key] = float(cfg[key])
            except (TypeError, ValueError):
                pass

    return cfg


def load_config(path: str):
    """
    Charge un fichier YAML de configuration et renvoie un dictionnaire Python.
    """
    # Normalisation du chemin
    abs_path = os.path.abspath(path)

    # Racine du projet (src/utils/.. → projet)
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    # Protection anti path traversal : le fichier doit rester dans le projet
    if not abs_path.startswith(project_root):
        raise Exception("Config path escapes project directory")

    with open(abs_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    return _coerce_config(cfg)
