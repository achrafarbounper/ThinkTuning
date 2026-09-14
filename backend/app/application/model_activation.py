"""Activation explicite d'une version de modele (SCRUM-55).

Un pointeur experiments/models/active.json (env ACTIVE_MODEL_POINTER) designe la
version active parmi les versions valides. /predict et resolve_model_dir(None)
resolvent desormais vers cette version active; sinon ils retombent sur la derniere
version valide.
"""

import json
import logging
import os

from app.domain.ports.model_activation_ports import get_model_activation_port

logger = logging.getLogger(__name__)

# Racine locale du catalogue : lue par ``read_version_f1`` (rapport
# d'entraînement de la version). Patchable indépendamment par les tests
# (``monkeypatch.setattr(model_activation, "MODEL_ROOT", ...)``) — parité avec
# l'import historique depuis ``model_versioning``.
MODEL_ROOT = os.path.join("experiments", "models")
DEFAULT_ACTIVE_POINTER = os.path.join("experiments", "models", "active.json")


def get_active_pointer_path() -> str:
    """Chemin du pointeur actif — délégué au port (ADR-0003 §3, B-3)."""
    return get_model_activation_port().get_active_pointer_path()


def read_active_pointer() -> dict | None:
    """Lit le pointeur actif (via le port). Retourne le dict parsable ou None si absent/corrompu."""
    return get_model_activation_port().read_active_pointer()


def write_active_pointer(version: str, path: str, f1_macro: float | None = None) -> dict:
    """Ecrit le pointeur actif (via le port — atomique : tmp + rename).."""
    return get_model_activation_port().write_active_pointer(version, path, f1_macro)


def is_valid_version(version: str) -> bool:
    """True si la version est un dossier valide (poids non vides).."""
    port = get_model_activation_port()
    if version not in port.list_model_versions():
        return False
    version_dir = os.path.join(port.model_root(), version)
    return os.path.isdir(version_dir)


def activate_model(version: str) -> dict:
    """Active une version valide.apres validation de l'entrainement de la tete.

    Leve ValueError si la version n'existe pas ou si la tete de classification n'est
    pas entrainee (seuil d'ecart-type > 0.03)..
    """
    if not is_valid_version(version):
        raise ValueError(f"Version de modele inconnue : {version}.")
    version_dir = os.path.join(MODEL_ROOT, version)
    if not get_model_activation_port().is_model_version_trained(version_dir):
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
        with open(report, encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("metrics", {}).get("f1_macro")
    except Exception:
        return None


def get_active_model_dir() -> str | None:
    """Chemin du dossier de la version active, ou None si aucune active (via le port)."""
    return get_model_activation_port().get_active_model_dir()


def is_active(version: str) -> bool:
    """True si la version designee est la version active courante."""
    data = read_active_pointer()
    return bool(data and data.get("version") == version)
