# project/core/model_sanity.py
"""Sanity check comportemental du modèle de sentiment (SCRUM-74).

Exécute ``Predictor.predict()`` sur un jeu fixe de phrases FR/EN clairement
polarisées afin de détecter :
  - un modèle NON ENTRAÎNÉ (bug SCRUM-55 : ``neutral`` universel avec une
    confidence ≈ 1/3 sur toutes les classes) ;
  - un FALLBACK BASE MODEL (le prédicteur est retombé sur une tête de
    classification non entraînée / un modèle de base zéro-shot).

L'objectif est que l'API retourne une erreur explicite (503) au lieu de
répondre normalement avec un modèle cassé, ce qui contaminerait le dataset
via l'active learning.
"""

import logging
import os

logger = logging.getLogger(__name__)

# Seuil par défaut : une confidence max < 0.4 sur toutes les classes de toutes
# les phrases signale un modèle quasi-aléatoire (softmax ≈ uniforme ≈ 1/3).
DEFAULT_MIN_CONFIDENCE = 0.4

# Verdicts possibles du sanity check.
VERDICT_OK = "ok"
VERDICT_UNTRAINED = "untrained"
VERDICT_FALLBACK = "fallback_base_model"

# Précision minimale attendue sur les phrases de référence pour considérer
# qu'un modèle n'est pas un fallback incohérent.
MIN_ACCURACY = 0.5

# ---------------------------------------------------------------------------
# Jeu de phrases de référence versionné dans le code (FR/EN, positif/négatif).
# Volontairement clairement polarisées : un modèle sain doit les classer sans
# ambiguïté. Toute confusion systématique est un signal de modèle cassé.
# ---------------------------------------------------------------------------
SANITY_PHRASES = [
    {"text": "Ce produit est absolument fantastique, je suis très satisfait !", "lang": "fr", "expected": "positive"},
    {"text": "Service client déplorable, je suis extrêmement déçu.", "lang": "fr", "expected": "negative"},
    {"text": "Quelle expérience merveilleuse, tout était parfait, merci beaucoup !", "lang": "fr", "expected": "positive"},
    {"text": "Qualité catastrophique, une vraie perte de temps et d'argent.", "lang": "fr", "expected": "negative"},
    {"text": "Absolutely fantastic experience, I love it, best purchase ever!", "lang": "en", "expected": "positive"},
    {"text": "Terrible quality, complete waste of money. I hate it.", "lang": "en", "expected": "negative"},
    {"text": "The team was amazing and the food was delicious, highly recommended.", "lang": "en", "expected": "positive"},
    {"text": "Awful support, the app keeps crashing and nobody helps. Very angry.", "lang": "en", "expected": "negative"},
]


def resolve_min_confidence() -> float:
    """Seuil configurable : env var ``MODEL_SANITY_MIN_CONFIDENCE`` >
    clé ``model_sanity_min_confidence`` de configs/default.yaml >
    défaut 0.4."""
    env_value = os.getenv("MODEL_SANITY_MIN_CONFIDENCE")
    if env_value:
        try:
            return float(env_value)
        except ValueError:
            logger.warning(
                "MODEL_SANITY_MIN_CONFIDENCE invalide (%r) ; valeur ignorée.",
                env_value,
            )
    try:
        from src.utils.config import load_config

        cfg = load_config("configs/default.yaml")
        return float(cfg.get("model_sanity_min_confidence", DEFAULT_MIN_CONFIDENCE))
    except Exception:
        return DEFAULT_MIN_CONFIDENCE


def _is_head_trained(predictor) -> bool | None:
    """True si le dossier du modèle chargé atteste un entraînement réel
    (training_report.json avec métriques / std de tête suffisant).
    None si indéterminable (stub de test, chemin inconnu...)."""
    model_dir = getattr(predictor, "model_path", None)
    if not model_dir or not os.path.isdir(model_dir):
        return None
    try:
        from core.model_head_check import is_model_version_trained

        return is_model_version_trained(model_dir)
    except Exception:
        return None


def run_model_sanity(predictor, min_confidence: float | None = None) -> dict:
    """Exécute ``predictor.predict()`` sur les phrases de référence et
    retourne un rapport dict :
        {
            "status": "ok" | "unhealthy",
            "verdict": "ok" | "untrained" | "fallback_base_model",
            "detail": str,
            "min_confidence": float,
            "accuracy": float,
            "results": [{text, lang, expected, predicted, confidence, correct}],
        }
    """
    if min_confidence is None:
        min_confidence = resolve_min_confidence()

    texts = [p["text"] for p in SANITY_PHRASES]
    preds = predictor.predict(texts)

    results = []
    for phrase, pred in zip(SANITY_PHRASES, preds):
        results.append({
            "text": phrase["text"],
            "lang": phrase["lang"],
            "expected": phrase["expected"],
            "predicted": pred["sentiment"],
            "confidence": float(pred["confidence"]),
            "correct": pred["sentiment"] == phrase["expected"],
        })

    n = len(results)
    n_correct = sum(1 for r in results if r["correct"])
    accuracy = n_correct / n if n else 0.0
    max_conf_overall = max((r["confidence"] for r in results), default=0.0)

    head_trained = _is_head_trained(predictor)

    # 1) Modèle non entraîné : confidence max < seuil sur TOUTES les phrases
    #    (softmax quasi uniforme ≈ 1/3 par classe — bug SCRUM-55).
    if n and max_conf_overall < min_confidence:
        return {
            "status": "unhealthy",
            "verdict": VERDICT_UNTRAINED,
            "detail": (
                "Modèle non entraîné détecté : confidence max "
                f"{max_conf_overall:.3f} < seuil {min_confidence:.2f} sur toutes "
                "les classes (softmax quasi uniforme ≈ 1/3). Ré-entraînez le "
                "modèle (POST /train) avant d'utiliser l'API."
            ),
            "min_confidence": min_confidence,
            "accuracy": accuracy,
            "results": results,
        }

    # 2) Fallback base model : le prédicteur se trompe massivement et/ou la
    #    tête de classification du dossier chargé n'est pas entraînée
    #    (le Predictor est retombé sur une version de secours / modèle de base).
    head_not_trained = head_trained is False
    if head_not_trained or (n and accuracy < MIN_ACCURACY):
        reasons = []
        if head_not_trained:
            reasons.append(
                "aucun entraînement attesté pour la version chargée "
                "(tête de classification non entraînée)"
            )
        if accuracy < MIN_ACCURACY:
            reasons.append(
                f"précision {accuracy:.0%} < {MIN_ACCURACY:.0%} sur les "
                f"{n} phrases de référence"
            )
        return {
            "status": "unhealthy",
            "verdict": VERDICT_FALLBACK,
            "detail": (
                "Fallback base model détecté : " + " ; ".join(reasons) + ". "
                "Le modèle chargé semble être un modèle de base / de secours. "
                "Ré-entraînez (POST /train) ou activez une autre version."
            ),
            "min_confidence": min_confidence,
            "accuracy": accuracy,
            "results": results,
        }

    # 3) Modèle sain.
    return {
        "status": "ok",
        "verdict": VERDICT_OK,
        "detail": (
            f"Sanity check OK : {n_correct}/{n} phrases correctement classées "
            f"(confidence max {max_conf_overall:.3f})."
        ),
        "min_confidence": min_confidence,
        "accuracy": accuracy,
        "results": results,
    }

