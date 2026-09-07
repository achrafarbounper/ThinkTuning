# project/api/schemas/prediction.py

import os
from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

# ---------------------------------------------------------------------------
# Garde-fous d'entrée : mêmes variables d'environnement que le legacy
# POST /predict (bornes anti-DoS ajustables à la capacité mémoire du
# déploiement). Lues à l'import du schéma : cohérent avec le legacy.
# ---------------------------------------------------------------------------
MAX_TEXTS_PER_REQUEST = int(os.getenv("PREDICT_MAX_TEXTS", "256"))
MAX_TEXT_CHARS = int(os.getenv("PREDICT_MAX_TEXT_CHARS", "10000"))


class PredictRequest(BaseModel):
    """Corps de POST /api/v1/predict.

    ``texts`` : liste NON VIDE (refuse [] qui ferait planter le tokenizer)
    et bornée (anti-DoS), comme le legacy.
    ``model_name`` : version cible optionnelle (défaut : version active).
    """

    texts: list[Annotated[str, StringConstraints(max_length=MAX_TEXT_CHARS)]] = Field(
        min_length=1,
        max_length=MAX_TEXTS_PER_REQUEST,
    )
    model_name: str | None = Field(
        default=None,
        description="Version de modèle cible (dossier sous experiments/models). "
        "Absente => version active.",
    )


class PredictedText(BaseModel):
    """Prédiction d'UNE phrase (shape legacy préservé + version enrichie)."""

    text: str
    sentiment: str
    confidence: float
    model_version: str | None = None


class PredictResponse(BaseModel):
    """Réponse de POST /api/v1/predict."""

    results: list[PredictedText]
    model_version: str | None = Field(
        default=None,
        description="Version de modèle utilisée (première prédiction du lot).",
    )
