"""Activation explicite d'une version de modele (SCRUM-55).

Un pointeur experiments/models/active.json (env ACTIVE_MODEL_POINTER) designe la
version active parmi les versions valides. /predict et resolve_model_dir(None)
resolvent desormais vers cette version active; sinon ils retombent sur la derniere
version valide.
"""

import os
import json
import logging
from datetime import datetime, timezone

from core.model_versioning import MODEL_ROOT, list_model_versions, resolve_model_dir
from core.model_head_check import is_model_version_trained

logger = logging.getLogger(__name__)

DEFAULT_ACTIVE_POINTER = os.path.join("experiments", "models", "active.json")


def get_active_pointer_path() -> str:
    return os.getenv("ACTIVE_MODEL_POINTER", DEFAULT_ACTIVE_POINTER)


def read_active_pointer() -> dict | None:
    """Lit le pointeur actif. Retourne le dict parsable ou None si absent/corrompu."""
    path = get_active_pointer_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("version"):
            return data
    except Exception as exc:
        logger.warning("Pointeur actif %s illisible : %s", path, exc)
    return None


def write_active_pointer(version: str, path: str, f1_macro: float | None = None) -> dict:
    """Ecrit le pointeur actif (atomique : tmp + rename).."""
    path_ptr = get_active_pointer_path()
    os.makedirs(os.path.dirname(path_ptr) or ".", exist_ok=True)
    data = {
        "version": version,
        "path": os.path.abspath(path),
        "f1_macro": float(f1_macro) if f1_macro is not None else None,
        "activated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    tmp = path_ptr + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path_ptr)
    logger.info("Modele actif -> %s (%s", version, path)
    return data


def is_valid_version(version: str) -> bool:
    """True si la version est un dossier valide (poids non vides).."""
    if version not in list_model_versions():
        return False
    version_dir = os.path.join(MODEL_ROOT, version)
    return os.path.isdir(version_dir)


def activate_model(version: str) -> dict:
    """Active une version valide.apres validation de l'entrainement de la tete.

    Leve ValueError si la version n'existe pas ou si la tete de classification n'est
    pas entrainee (seuil d'ecart-type > 0.03)..
    """
    if not is_valid_version(version):
        raise ValueError(f"Version de modele inconnue : {version}.")
    version_dir = os.path.join(MODEL_ROOT, version)
    if not is_model_version_trained(version_dir):
        raise ValueError(
            f"Version {version} non activable : tete de classification non entrainee"
            " (ecart-type <= 0.03)ou poids absents."
        )
    f1 = read_version_f1(version)
    return write_active_pointer(version, version_dir, f1)


def read_version_f1(version: str) -> float | None:
    """Lit le f1_macro du rapport d'entrainement d'une version (ou None)."""
    report = os.path.join(MODEL_ROOT, version, "training_report.json")
    if not os.path.isfile(report):
        return None
    try:
        with open(report, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("metrics", {}).get("f1_macro")
    except Exception:
        return None


def get_active_model_dir() -> str | None:
    """Chemin du dossier de la version active, ou None si aucune active."""
    data = read_active_pointer()
    if data and os.path.isdir(data.get("path", "")):
        return data["path"]
    return None


def is_active(version: str) -> bool:
    """True si la version designee est la version active courante."""
    data = read_active_pointer()
    return bool(data and data.get("version") == version)
