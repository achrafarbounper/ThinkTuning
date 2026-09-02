# project/api/routes/models.py

import os
import re
import json
import shutil
import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException
import api
from api.dependencies.auth import require_api_key
from core.model_versioning import list_model_versions, resolve_model_dir, validate_model_version, MODEL_ROOT
from core.model_activation import activate_model, read_active_pointer, is_active
from core.model_sanity import run_model_sanity, VERDICT_OK
from core.predictor_cache import get_predictor, evict_cached_model
from core.models import ModelVersion

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/models", tags=["Models"])

# Nom de version autorisé : caractères sûrs uniquement (anti path traversal).
_VERSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@router.post("/{name}/activate")
def activate_model_version(name: str, _: bool = Depends(require_api_key)):
    """Active une version de modele apres validation complete de ses artefacts.

    Rejette (422) toute version invalide : config.json, poids, mappings
    id2label/label2id, tete de classification entrainee (std > 0.03).
    """
    try:
        # Validation complete avant activation (SCRUM-55).
        validate_model_version(os.path.join(MODEL_ROOT, name))
        pointer = activate_model(name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"activated": True, **pointer}


@router.get("/active")
def get_active_version(_: bool = Depends(require_api_key)):
    """Pointeur de la version actuellement active (ou null si aucune)."""
    return read_active_pointer() or {"activated": False}

@router.get("", tags=["Models"])
def list_models(_: bool = Depends(require_api_key)):
    root = api.MODEL_ROOT

    items = []
    for name in sorted(os.listdir(root), reverse=True):
        path = os.path.join(root, name)

        if os.path.isdir(path) and os.path.isfile(os.path.join(path, "training_report.json")):
            items.append({
                "name": name,
                "path": os.path.abspath(path)
            })

    return items


@router.get("/details", response_model=List[ModelVersion])
def list_models_details(_: bool = Depends(require_api_key)):
    """Renvoie la liste des modèles enregistrés, du plus récent au plus ancien."""
    model_versions = []
    # Aucun modèle entraîné (ex: premier lancement de l'image Docker avec
    # /app/experiments/models vide) -> renvoyer une liste vide (200) plutôt
    # qu'un 500 RuntimeError. Cohérent avec /predict qui renvoie 503.
    try:
        active_model_path = os.path.abspath(resolve_model_dir())
    except RuntimeError:
        active_model_path = None
    for name in list_model_versions():
        path = os.path.abspath(os.path.join(MODEL_ROOT, name))
        model_versions.append(
            ModelVersion(
                name=name,
                path=path,
                created_at=os.path.getmtime(path),
                active=(path == active_model_path),
            )
        )
    return model_versions


@router.get("/{name}/report")
def get_model_report(name: str, _: bool = Depends(require_api_key)):
    """Retourne le rapport JSON associé à une version de modèle."""
    version_dir = os.path.join(MODEL_ROOT, name)
    report_path = os.path.join(version_dir, "training_report.json")

    if not os.path.isfile(report_path):
        raise HTTPException(
            status_code=404,
            detail=f"Training report not found for model '{name}'."
        )

    with open(report_path, "r", encoding="utf-8") as fh:
        return json.load(fh)


# Fichiers possibles pour un tokenizer sauvegardé (save_pretrained).
_TOKENIZER_FILES = (
    "tokenizer_config.json",
    "tokenizer.json",
    "vocab.txt",
    "vocab.json",
    "spiece.model",
    "merges.txt",
)

# Fichiers de poids considérés non vides.
_WEIGHT_FILES = ("model.safetensors", "pytorch_model.bin", "model.pt", "model_state_dict.pt")


def _structural_invalidity_reason(version_dir: str) -> str | None:
    """Vérification structurelle SANS chargement de modèle.

    Retourne la raison pour laquelle la version est incomplète (None si la
    structure est a priori exploitable) :
      - aucun fichier tokenizer non vide ;
      - config.json absent, vide ou non parsable ;
      - aucun fichier de poids non vide.
    Permet au DELETE de supprimer immédiatement une version invalide sans
    instancier de Predictor ni lancer le sanity check comportemental.
    """
    if not os.path.isdir(version_dir):
        return "dossier de version inexistant"

    if not any(
        os.path.isfile(os.path.join(version_dir, f)) and os.path.getsize(os.path.join(version_dir, f)) > 0
        for f in _TOKENIZER_FILES
    ):
        return "aucun fichier tokenizer"

    config_path = os.path.join(version_dir, "config.json")
    if not os.path.isfile(config_path):
        return "config.json absent"
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            config = json.load(fh)
        if not isinstance(config, dict) or not config:
            return "config.json vide"
    except (ValueError, OSError) as exc:
        return f"config.json illisible : {exc}"

    if not any(
        os.path.isfile(os.path.join(version_dir, f)) and os.path.getsize(os.path.join(version_dir, f)) > 0
        for f in _WEIGHT_FILES
    ):
        return "aucun fichier de poids non vide"

    return None


@router.delete("/{name}")
def delete_model_version(name: str, _: bool = Depends(require_api_key)):
    """Supprime une version de modèle défaillante de experiments/models.

    Deux niveaux de décision :
      1. Vérification structurelle SANS chargement de modèle : une version
         incomplète (pas de tokenizer, config.json absent/vide/illisible,
         aucun poids non vide) est supprimée immédiatement (verdict
         « model_unavailable ») — sans get_predictor(), sans
         run_model_sanity(), sans risque de 500.
      2. Sinon, sanity check comportemental (run_model_sanity) décide :
         - verdict « untrained » / « fallback_base_model » : suppression ;
         - verdict « ok » (modèle sain) : refus 422 ;
         - version active : refus 409, quel que soit le verdict.
    """
    if not _VERSION_NAME_RE.match(name) or ".." in name:
        raise HTTPException(status_code=422, detail=f"Nom de version invalide : {name!r}")

    version_dir = os.path.join(MODEL_ROOT, name)
    if not os.path.isdir(version_dir):
        raise HTTPException(status_code=404, detail=f"Version de modèle inconnue : {name}.")

    if is_active(name):
        raise HTTPException(
            status_code=409,
            detail=(
                f"La version {name} est le modèle actif : désactivez-la ou "
                "activez une autre version avant suppression."
            ),
        )

    # 1) Version structurellement invalide -> suppression immédiate, sans
    #    charger le moindre modèle (ni Predictor, ni sanity check).
    invalid_reason = _structural_invalidity_reason(version_dir)
    if invalid_reason:
        verdict, detail, accuracy = "model_unavailable", invalid_reason, None
    else:
        # 2) Sanity check comportemental sur cette version précise.
        try:
            predictor = get_predictor(name)
            report = run_model_sanity(predictor)
            verdict = report["verdict"]
            detail = report["detail"]
            accuracy = report["accuracy"]
        except HTTPException as exc:
            # Dossier cassé / incomplet / modèle illisible -> « model_unavailable »,
            # ce qui autorise le nettoyage (cas d'usage principal).
            verdict = "model_unavailable"
            detail = str(exc.detail)
            accuracy = None

    if verdict == VERDICT_OK:
        raise HTTPException(
            status_code=422,
            detail=(
                f"La version {name} est saine (sanity check ok) : suppression "
                "refusée. Seules les versions défaillantes peuvent être nettoyées."
            ),
        )

    # Libérer le prédicteur éventuellement chargé pour cette version, puis
    # supprimer le dossier.
    evict_cached_model(name)
    shutil.rmtree(version_dir)
    logger.info("Version de modèle supprimée : %s (verdict=%s)", name, verdict)

    return {
        "deleted": True,
        "name": name,
        "verdict": verdict,
        "detail": detail,
        "accuracy": accuracy,
    }
