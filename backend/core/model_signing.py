# project/core/model_signing.py
"""Signature et vérification d'intégrité des modèles (P2 lot 15).

Supply-chain ML : chaque dossier de version modèle publié dans
``experiments/models/<version>`` porte un manifeste ``sha256.json`` listant
l'empreinte SHA-256 de CHAQUE fichier (poids, tokenizer, config, mappings) :

    {
      "version": 1,
      "files": {
        "model.safetensors": "0ab9...",
        "config.json": "91c2...",
        "vocab.txt": "dd0e...",
        ...
      }
    }

Garanties :
    - ``verify_model_signature(model_dir)`` est appelé au CHARGEMENT
      (``src/inference/predictor.py``) AVANT ``from_pretrained`` : un modèle
      altéré (poids modifiés, fichier injecté ou supprimé) est REFUSÉ
      (``ModelSignatureError``) — fail-closed si le manifeste existe ;
    - ``write_signature_manifest(model_dir)`` est appelé après la publication
      d'une version (``core/model_versioning._save_trained_model``) ;
    - vérification déterministe et hors-ligne (aucun appel réseau) ;
    - ``trust_remote_code=False`` est appliqué systématiquement aux appels
      ``from_pretrained`` (interdiction d'exécuter du code téléchargé).

Le manifeste étant lui-même un fichier du dossier, la menace résiduelle est
un attaquant capable d'écrire dans ``experiments/models`` : il pourrait
régénérer le manifeste. La défense en profondeur (et l'exigence « signature
modèles ») repose sur l'hébergement : ``experiments/models`` est en lecture
seule hors écriture d'entraînement, et la red-team trimestrielle re-vérifie
les versions contre des empreintes conservées hors-ligne (scripts/redteam).
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

MANIFEST_NAME = "sha256.json"
_CHUNK = 1 << 20  # 1 Mo par lecture (mémoire bornée sur fichiers multi-Go)


class ModelSignatureError(RuntimeError):
    """Empreinte absente / incohérente au chargement (modèle à risque)."""


def sha256_file(path: Path) -> str:
    """Empreinte SHA-256 d'un fichier (streaming, mémoire bornée)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
def _payload_files(model_dir: Path) -> list[Path]:
    """Fichiers signés : tout fichier EXCEPTÉ le manifeste lui-même et les
    temporaires (``*.tmp``, dossiers ``.tmp``)."""
    return [
        p
        for p in sorted(model_dir.rglob("*"))
        if p.is_file()
        and p.name != MANIFEST_NAME
        and ".tmp" not in p.parts
        and not p.name.endswith(".tmp")
    ]


def write_signature_manifest(model_dir: str | Path) -> str:
    """Écrit (ou réécrit) ``sha256.json`` pour un dossier de version modèle.

    Retourne le chemin du manifeste écrit.
    """
    model_dir = Path(model_dir)
    if not model_dir.is_dir():
        raise ModelSignatureError(f"dossier modèle introuvable : {model_dir}")
    files = {
        str(p.relative_to(model_dir)).replace("\\", "/"): sha256_file(p)
        for p in _payload_files(model_dir)
    }
    manifest = {"version": 1, "files": files}
    manifest_path = model_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    logger.info(
        "Manifeste de signature écrit : %s (%d fichier(s))", manifest_path, len(files)
    )
    return manifest_path.as_posix()


def load_signature_manifest(model_dir: str | Path) -> dict | None:
    """Charge ``sha256.json`` (None si absent)."""
    manifest_path = Path(model_dir) / MANIFEST_NAME
    if not manifest_path.is_file():
        return None
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ModelSignatureError(f"manifeste {manifest_path} illisible : {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ModelSignatureError(f"manifeste {manifest_path} au format invalide")
    return data


def verify_model_signature(model_dir: str | Path, *, required: bool | None = None) -> dict:
    """Vérifie l'intégrité d'un dossier de version contre ``sha256.json``.

    - manifeste absent : ``required=True`` (ou policy
      ``MODEL_SIGNING_REQUIRED`` via ``is_signing_required()``) ->
      ``ModelSignatureError`` (fail-closed), sinon log warning ;
    - fichier manquant/ajouté OU empreinte différente -> ``ModelSignatureError``
      (refus du chargement).

    Retourne ``{"ok": bool, "checked": bool, "files": n, "errors": [...]}``.
    """
    if required is None:
        required = is_signing_required()
    model_dir = Path(model_dir)
    manifest = load_signature_manifest(model_dir)
    if manifest is None:
        message = (
            f"Manifeste de signature absent : {model_dir}/{MANIFEST_NAME} "
            "(MODEL_SIGNING_REQUIRED=1 en exige un -> chargement refusé)."
        )
        if required:
            raise ModelSignatureError(message)
        logger.warning(message)
        return {"ok": True, "checked": False, "files": 0, "errors": []}

    expected = manifest["files"]
    errors: list[str] = []
    for rel, digest in expected.items():
        path = Path(model_dir) / rel
        if not path.is_file():
            errors.append(f"fichier absent : {rel}")
            continue
        if sha256_file(path) != digest:
            errors.append(f"empreinte incohérente : {rel}")

    # Fichier présent sur disque mais non signé (injection) — fail-closed.
    disk_files = {
        str(p.relative_to(model_dir)).replace("\\", "/") for p in _payload_files(model_dir)
    }
    unsigned = sorted(disk_files - set(expected))
    if unsigned:
        errors.append(f"fichier(s) non signé(s) : {', '.join(unsigned[:5])}")

    if errors:
        raise ModelSignatureError(
            f"intégrité modèle refusée ({len(errors)} écart(s)) : {errors[:10]}"
        )
    logger.info(
        "Signature modèle vérifiée : %s (%d fichier(s), %d empreintes)",
        model_dir,
        len(disk_files),
        len(expected),
    )
    return {"ok": True, "checked": True, "files": len(disk_files), "errors": []}


def is_signing_required() -> bool:
    """Policy d'exigence (MODEL_SIGNING_REQUIRED, défaut désactivé pour ne pas
    casser les déploiements legacy ; recommandé en production)."""
    from os import getenv

    return getenv("MODEL_SIGNING_REQUIRED", "0").lower() in {"1", "true", "yes"}


__all__ = [
    "MANIFEST_NAME",
    "ModelSignatureError",
    "is_signing_required",
    "load_signature_manifest",
    "sha256_file",
    "verify_model_signature",
    "write_signature_manifest",
]
