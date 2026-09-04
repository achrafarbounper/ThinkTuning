# project/core/predictor_cache.py

import os
import threading
from collections import OrderedDict

from fastapi import HTTPException

from core.model_versioning import resolve_model_dir
from src.inference.predictor import Predictor

# Nombre maximal de versions de modèles gardées en mémoire (LRU).
# Un cache multi-slots évite le « thrashing » : avec l'ancien cache mono-slot,
# alterner deux modèles rechargeait l'intégralité des poids à CHAQUE requête.
_MAX_CACHED_PREDICTORS = 3

# _cache[chemin_absolu] = Predictor ; OrderedDict -> éviction LRU en O(1).
_cache: OrderedDict[str, Predictor] = OrderedDict()
_cache_lock = threading.RLock()

# Compatibilité tests : miroir du prédicteur le plus récemment actif.
# (Les tests e2e sauvegardent/restaurent cette variable. Le code de
# production ne doit PAS la lire : passer par get_predictor().)
_predictor: Predictor | None = None


def _resolve_model_dir_or_503(model_name: str | None) -> str:
    try:
        model_dir = resolve_model_dir(model_name)
    except RuntimeError:
        model_dir = None
    if model_dir is None or not os.path.isdir(model_dir):
        raise HTTPException(status_code=503, detail="Aucun modèle disponible")
    return os.path.abspath(model_dir)


def _load_predictor(model_dir: str) -> Predictor:
    if not os.listdir(model_dir):
        raise HTTPException(status_code=503, detail="Aucun modèle disponible")
    return Predictor(model_dir)


def _store(path: str, predictor: Predictor) -> None:
    """Insertion LRU sous verrou (appelé après un chargement hors verrou)."""
    global _predictor
    with _cache_lock:
        _cache[path] = predictor
        _cache.move_to_end(path)
        while len(_cache) > _MAX_CACHED_PREDICTORS:
            _cache.popitem(last=False)
        _predictor = predictor


def get_predictor(model_name: str | None = None) -> Predictor:
    """Prédicteur d'une version de modèle (cache LRU thread-safe).

    Le verrou n'est plus tenu pendant le chargement des poids (plusieurs
    secondes) : seul l'accès au dict est verrouillé, donc les requêtes
    visant d'autres modèles restent servies pendant un rechargement.
    """
    model_dir = _resolve_model_dir_or_503(model_name)

    # Fast path (verrou court) : version déjà en cache.
    with _cache_lock:
        predictor = _cache.get(model_dir)
        if predictor is not None:
            _cache.move_to_end(model_dir)
            return predictor

    # Chargement HORS verrou : ne bloque plus l'API entière.
    predictor = _load_predictor(model_dir)
    _store(model_dir, predictor)
    return predictor


def reload_predictor() -> None:
    """Force le rechargement de la version active depuis le disque."""
    model_dir = _resolve_model_dir_or_503(None)
    predictor = _load_predictor(model_dir)
    _store(model_dir, predictor)


def evict_cached_model(model_name: str | None = None) -> bool:
    """Retire du cache le prédicteur d'une version (avant suppression disque).

    Libère les fichiers ouverts (Windows verrouille les fichiers en cours
    d'utilisation). Retourne True si une entrée a été évacuée.
    """
    global _predictor
    try:
        model_dir = resolve_model_dir(model_name)
    except RuntimeError:
        return False
    target = os.path.abspath(model_dir or "")
    if not target:
        return False

    removed = False
    with _cache_lock:
        for path in list(_cache):
            if os.path.abspath(path) == target:
                _cache.pop(path, None)
                removed = True
        if removed:
            # Le miroir de compat doit pointer vers un prédicteur encore
            # présent en cache (ou None si le cache est vide).
            _predictor = next(reversed(_cache.values()), None) if _cache else None
    return removed

