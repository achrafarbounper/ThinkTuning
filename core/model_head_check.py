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
    return _classifier_std(state) > HEAD_CLASSIFIER_MIN_STD