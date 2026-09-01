"""Heuristics to detect a genuinely trained classification head in a version.-"""
import os
import logging

logger = logging.getLogger(__name__)

MODEL_WEIGHT_FILES = [
    "model.pt",
    "pytorch_model.bin",
    "model.safetensors",
    "model_state_dict.pt",
]

HEAD_CLASSIFIER_KEYS = ("classifier", "head", "output")
HEAD_CLASSIFIER_MIN_STD = float("0.03")


def _has_completed_training_report(dirpath):
    """Un ``training_report.json`` écrit par ``save_model_version`` avec des
    métriques atteste que l'entraînement s'est réellement déroulé.

    Nécessaire car un classifier DistilBERT initialisé via ``normal(0, 0.02)``
    garde un écart-type ≈ 0.02 après un fine-tuning sur un petit dataset : le
    seul seuil de std (> 0.03) rejette alors TOUS les modèles légitimes."""
    report_path = os.path.join(dirpath, "training_report.json")
    if not os.path.isfile(report_path) or os.path.getsize(report_path) <= 0:
        return False
    try:
        import json

        with open(report_path, "r", encoding="utf-8") as fh:
            report = json.load(fh)
    except (ValueError, OSError):
        return False
    if not isinstance(report, dict):
        return False
    metrics = report.get("metrics") or {}
    return bool(metrics.get("accuracy_by_epoch") or metrics.get("f1_by_epoch"))

def _tensor_std(t):
    import torch
    try:
        return float(t.float().std().item())
    except Exception:
        return 0.0


def _classifier_std(state_dict):
    for name_stem in HEAD_CLASSIFIER_KEYS:
        for key, value in state_dict.items():
            parts = key.split(".")
            if len(parts) == 2:
                if parts[0] == name_stem:
                    if parts[1] == "weight":
                        return _tensor_std(value)
    return 0.0


def _load_head_state(dirpath):
    import torch
    for fname in MODEL_WEIGHT_FILES:
        fpath = os.path.join(dirpath, fname)
        if not os.path.isfile(fpath) or os.path.getsize(fpath) <= 0:
            continue
        try:
            if fname.endswith(".safetensors"):
                from safetensors import safe_open
                with safe_open(fpath, framework="pt") as handle:
                    head = {}
                    for key in handle.keys():
                        parts = key.split(".")
                        if len(parts) == 2:
                            if parts[0] in HEAD_CLASSIFIER_KEYS:
                                if parts[1] == "weight":
                                    head[key] = handle.get_tensor(key)
                    return head if head else {}
            else:
                sd = torch.load(fpath, map_location="cpu", weights_only=True)
                if isinstance(sd, dict):
                    return sd
                return {}
        except Exception:
            return {}
    return {}


def is_model_version_trained(dirpath):
    state = _load_head_state(dirpath)
    if not state:
        return False
    if _classifier_std(state) > HEAD_CLASSIFIER_MIN_STD:
        return True
    # Tête faiblement déplacée (fine-tuning court) mais entraînement attesté
    # par un training_report.json avec métriques -> considérée comme entraînée.
    return _has_completed_training_report(dirpath)