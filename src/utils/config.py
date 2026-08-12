import yaml


def _coerce_config(cfg):
    """Normalize config values that may be loaded as strings from YAML."""
    if not isinstance(cfg, dict):
        return cfg

    int_keys = {"max_length", "epochs", "batch_size", "num_workers"}
    float_keys = {"learning_rate", "weight_decay", "warmup_ratio"}

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
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    return _coerce_config(cfg)