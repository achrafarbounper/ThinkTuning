# project/api/routes/explain.py

"""Endpoint POST /explain — Explication LLM du sentiment prédit.

Prend un texte, le fait prédire par DistilBERT (dernière version valide via
``api._get_predictor``) puis demande à l'agent IA une explication en langage
naturel de la prédiction via le provider OpenRouter
(``core.agent_cache.ask_agent_openrouter``), la prédiction (sentiment +
confidence) servant de contexte.

Contrat d'entrée  : POST /explain  {"text": str, "model"?: str}
Contrat de sortie : {"sentiment": str, "confidence": float, "explanation": str}

Auth : en-tête X-API-Key (dépendance ``require_api_key``), comme les autres
routes de l'API.

Le champ ``model`` est le modèle LLM OpenRouter à utiliser pour l'explication
(défaut : « openrouter/free »). La clé OpenRouter est requise
(``OPENROUTER_API_KEY`` en env ou en base de paramètres).
"""

import api
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from api.dependencies.auth import require_api_key
from core import agent_cache

router = APIRouter(tags=["Explication"])


class ExplainRequest(BaseModel):
    text: str = Field(..., min_length=1, description="Texte à expliquer.")
    model: str | None = Field(
        None,
        description=(
            "Modèle LLM OpenRouter à utiliser pour l'explication (optionnel ; "
            "par défaut « openrouter/free »)."
        ),
    )


class ExplainResponse(BaseModel):
    sentiment: str
    confidence: float
    explanation: str


def build_explanation_prompt(text: str, sentiment: str, confidence: float) -> str:
    """Construit le prompt envoyé à l'agent IA pour expliquer la prédiction.

    La prédiction DistilBERT (sentiment + confidence) est injectée comme
    contexte afin que l'agent explique POURQUOI le modèle a classé le texte
    de cette façon, en langage naturel.
    """
    return (
        "Tu es un expert en analyse de sentiment. Un modèle DistilBERT a "
        "classé le texte ci-dessous et nous devons expliquer son résultat "
        "en langage naturel.\n\n"
        f"Texte analysé : \"{text}\"\n"
        f"Sentiment prédit par le modèle : {sentiment}\n"
        f"Confiance du modèle : {confidence:.2f} (entre 0 et 1)\n\n"
        "Explique clairement pourquoi le modèle a pu arriver à ce verdict : "
        "identifie les mots ou tournures qui justifient ce sentiment, signale "
        "l'éventuelle ambiguïté (sarcasme, négation, ironie) et nuance selon "
        "le niveau de confiance. Réponds uniquement avec l'explication, en "
        "quelques phrases."
    )


@router.post("/explain", response_model=ExplainResponse)
def explain_route(
    req: ExplainRequest,
    _: bool = Depends(require_api_key),
):
    # 1) Prédiction DistilBERT : sert de contexte pour l'explication.
    predictor = api._get_predictor()
    result = predictor.predict([req.text])[0]

    # 2) Explication en langage naturel via l'agent IA (provider OpenRouter).
    prompt = build_explanation_prompt(
        req.text,
        result["sentiment"],
        result["confidence"],
    )
    explanation = agent_cache.ask_agent_openrouter(prompt, req.model)

    return {
        "sentiment": result["sentiment"],
        "confidence": result["confidence"],
        "explanation": explanation,
    }