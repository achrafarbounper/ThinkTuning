# project/app/api/routes/explain.py

"""Endpoint POST /explain — Explication LLM du sentiment prédit.

Prend un texte, le fait prédire par DistilBERT (dernière version valide via
``app.api._get_predictor``) puis demande à l'agent IA une explication en langage
naturel de la prédiction via le provider OpenRouter
(``app.application.explain_agent.ask_agent_openrouter``), la prédiction (sentiment +
confidence) servant de contexte.

Contrat d'entrée  : POST /explain  {"text": str, "model"?: str}
Contrat de sortie : {"sentiment": str, "confidence": float, "explanation": str}

Auth : en-tête X-API-Key (dépendance ``require_api_key``), comme les autres
routes de l'API.

Le champ ``model`` est le modèle LLM OpenRouter à utiliser pour l'explication
(défaut : « openrouter/free »). La clé OpenRouter est requise
(``OPENROUTER_API_KEY`` en env ou en base de paramètres).

Migration legacy (S3) : ce module n'importe plus la façade strangler
``app.application.agent_cache`` ni le runtime v1 — le use-case
``app.application.explain_agent`` fournit l'explication via le noyau v2
(config typée ``AgentConfig`` + ``HttpLLMClient``).
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import app.api as api
from app.api.dependencies.auth import require_read_api_key
from app.application.explain_agent import ask_agent_openrouter

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
        f'Texte analysé : "{text}"\n'
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
    _: bool = Depends(require_read_api_key),  # P1 : lecture (aucune mutation)
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
    try:
        explanation = ask_agent_openrouter(prompt, req.model)
    except ValueError as exc:  # clé OpenRouter manquante (config invalidée)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=504,
            detail="Le LLM OpenRouter n'a pas répondu (timeout).",
        ) from exc
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=502,
            detail="LLM OpenRouter injoignable. Vérifiez la configuration OpenRouter.",
        ) from exc
    except httpx.HTTPStatusError as exc:
        detail = f"Erreur renvoyée par le LLM OpenRouter (HTTP {exc.response.status_code})."
        raise HTTPException(status_code=502, detail=detail) from exc
    except httpx.HTTPError as exc:  # autre erreur réseau httpx (protocole…)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "sentiment": result["sentiment"],
        "confidence": result["confidence"],
        "explanation": explanation,
    }
