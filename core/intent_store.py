"""Store des versions du modèle d'intention (Phase 4).

Parallèle minimal de ``core/model_versioning.py`` pour le système d'intention
(chat/action), qui vit dans ``experiments/intent_models/`` (le dossier
``experiments/models`` reste dédié au sentiment). Chaque version est un dossier
horodaté contenant ``config.json`` + ``model.safetensors`` (compatible
``transformers.AutoModelForSequenceClassification``). Un pointeur optionnel
``active.json`` désigne la version active ; à défaut, la dernière version
valide est utilisée.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger("thinktuning.core.intent_store")

INTENT_MODEL_ROOT = os.path.join("experiments", "intent_models")
_ACTIVE_FILE = "active.json"
_WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin", "model.pt")
_INTENT_LABELS = ("chat", "action")


def _is_valid_version(version_dir: Path) -> bool:
    """Vrai si le dossier contient un config parsible + des poids non vides."""
    if not version_dir.is_dir():
        return False
    config = version_dir / "config.json"
    if not config.is_file():
        return False
    try:
        with open(config, encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not data.get("_name_or_path"):
            return False
    except (ValueError, OSError):
        return False
    return any(
        (version_dir / fname).is_file() and (version_dir / fname).stat().st_size > 0
        for fname in _WEIGHT_FILES
    )


def _root() -> Path:
    root = Path(INTENT_MODEL_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def list_intent_model_versions() -> list[str]:
    """Versions valides, triées par nom décroissant (plus récente d'abord)."""
    versions = [
        entry.name
        for entry in _root().iterdir()
        if entry.is_dir() and _is_valid_version(entry)
    ]
    return sorted(versions, reverse=True)


def resolve_intent_model_dir(model_name: str | None = None) -> str:
    """Résout un dossier de modèle d'intention valide (str, prêt pour les classes).

    - ``model_name`` explicite → ``INTENT_MODEL_ROOT/<model_name>`` ;
    - sinon le pointeur ``active.json`` s'il pointe vers une version valide ;
    - sinon la dernière version valide ;
    - si rien n'est disponible → RuntimeError (le fallback règles prend le relais).
    """
    root = _root()
    if model_name:
        candidate = root / model_name
        if not _is_valid_version(candidate):
            raise RuntimeError(
                f"Version du modèle d'intention {model_name!r} introuvable ou "
                f"invalide dans {root}."
            )
        return str(candidate)

    # Pointeur actif (même convention que core/model_activation).
    active_file = root / _ACTIVE_FILE
    if active_file.is_file():
        try:
            with open(active_file, encoding="utf-8") as fh:
                data = json.load(fh)
            name = data.get("active", "")
            if name and (root / name).is_dir() and _is_valid_version(root / name):
                return str(root / name)
        except (ValueError, OSError) as exc:
            logger.warning("Pointeur actif illisible (%s) ; repli dernière version.", exc)

    versions = list_intent_model_versions()
    if not versions:
        raise RuntimeError(
            f"Aucun modèle d'intention dans {root.absolute()} — "
            "entraînez-le d'abord (scripts/train_intent.py) ou laissez le "
            "fallback de règles actif."
        )
    return str(root / versions[0])


def set_active_intent_version(model_name: str) -> bool:
    """Écrit le pointeur ``active.json`` vers une version existante."""
    root = _root()
    if not _is_valid_version(root / model_name):
        raise RuntimeError(f"Version d'intention {model_name!r} invalide.")
    payload = {
        "active": model_name,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    with open(root / _ACTIVE_FILE, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return True


def default_intent_labels() -> tuple[str, ...]:
    """Labels fixes du classifieur d'intention (ordre de la tête de sortie)."""
    return _INTENT_LABELS
