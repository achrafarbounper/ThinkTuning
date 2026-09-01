# project/core/predictor_cache.py

import threading
from core.model_versioning import resolve_model_dir
from src.inference.predictor import Predictor
from fastapi import HTTPException
import os

_predictor = None
_predictor_path = None
_predictor_lock = threading.Lock()


def get_predictor(model_name: str | None = None):
    global _predictor, _predictor_path
    with _predictor_lock:
        try:
            model_dir = resolve_model_dir(model_name)
        except RuntimeError:
            model_dir = None

        # Aucun modèle disponible → renvoyer 503
        if model_dir is None or not os.path.exists(model_dir):
            raise HTTPException(
                status_code=503,
                detail="Aucun modèle disponible"
            )

        # Vérifier que le dossier contient un modèle utilisable
        if not os.listdir(model_dir):
            raise HTTPException(
                status_code=503,
                detail="Aucun modèle disponible"
            )

        # Recharger si aucun modèle n'est en cache ou si un autre modèle est demandé
        if _predictor is None or _predictor_path != model_dir:
            _predictor = Predictor(model_dir)
            _predictor_path = model_dir

        return _predictor


def reload_predictor():
    global _predictor, _predictor_path
    with _predictor_lock:
        try:
            model_dir = resolve_model_dir()
        except RuntimeError:
            model_dir = None

        if model_dir is None or not os.path.exists(model_dir) or not os.listdir(model_dir):
            raise HTTPException(status_code=503, detail="Aucun modèle disponible")

        _predictor = Predictor(model_dir)
        _predictor_path = model_dir


def evict_cached_model(model_name: str | None = None) -> bool:
    """Retire du cache le prédicteur correspondant à une version (si chargée).

    Utilisé avant suppression d'un dossier de modèle afin de libérer les
    fichiers ouverts (Windows verrouille les fichiers en cours d'utilisation).
    Retourne True si une entrée de cache a été évacuée.
    """
    global _predictor, _predictor_path
    with _predictor_lock:
        if _predictor is None or _predictor_path is None:
            return False
        try:
            model_dir = resolve_model_dir(model_name)
        except RuntimeError:
            return False
        if os.path.abspath(model_dir or "") == os.path.abspath(_predictor_path):
            _predictor = None
            _predictor_path = None
            return True
        return False
