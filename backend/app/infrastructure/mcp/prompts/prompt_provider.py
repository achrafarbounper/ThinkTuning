# project/app/infrastructure/mcp/prompts/prompt_provider.py
"""Provider MCP des prompts ThinkTuning — tâche 9 (S3, v1.0.0 Beta).

Implémente le port domaine ``MCPPromptRegistryPort`` (tâche 3) : chaque prompt
est un template nommé résolu LOCALEMENT — aucune I/O, aucun appel tool, aucun
appel LLM (catalogue statique possédée par le serveur) :

    name               → template                                    → argument
    analyze-sentiment  → "Analyse le sentiment de ce texte: {text}"      → text (requis)
    plan-training      → "Planifie un entraînement pour: {dataset}"      → dataset (requis)

SÉCURITÉ (checklist tâche 9) — fail-closed :

    1. nom : tout prompt non déclaré → ``NotFoundError`` (message actionnable :
       le catalogue est public via ``prompts/list``, lister les noms ne fuit
       rien) ;
    2. arguments : tout argument REQUIS manquant → ``ValidationError``
       (client-réparable, 422) ; toute valeur non-string → ``ValidationError``
       (la spec MCP ne transporte que des chaînes — on n'interpole JAMAIS un
       objet arbitraire du serveur) ;
    3. interpolation : les arguments sont des VALEURS substituées dans un
       template STATIQUE possédé par le serveur (``str.format``) — jamais
       l'inverse (le client ne contrôle pas le gabarit : pas d'accès
       attribut/index inattendu) et les valeurs substituées ne sont PAS
       re-traitées comme des gabarits (pas de récursion) ;
    4. arguments supplémentaires : ignorés (surplus toléré par la spec — pas
       d'oracle d'erreur différentiel).

Erreurs : ``NotFoundError`` (prompt inconnu) et ``ValidationError`` (argument
requis manquant, valeur non-string) — toutes deux ``DomainError``, traduites
en ``Invalid params`` (-32602) par le serveur MCP ; en régime nominal la
résolution est pure (aucune autre exception).
"""

from __future__ import annotations

import logging
from typing import Any

from app.domain.entities.mcp import (
    MCPPromptArgument,
    MCPPromptMessage,
    MCPPromptTemplate,
)
from app.domain.errors import NotFoundError, ValidationError
from app.domain.ports.mcp_ports import MCPPromptRegistryPort

logger = logging.getLogger("thinktuning.mcp.prompts")

__all__ = [
    "PROMPT_ANALYZE_SENTIMENT",
    "PROMPT_PLAN_TRAINING",
    "PromptProvider",
    "build_prompt_provider",
]

# Noms des prompts (identifiants MCP stables — checklist tâche 9).
PROMPT_ANALYZE_SENTIMENT = "analyze-sentiment"
PROMPT_PLAN_TRAINING = "plan-training"

# Templates possédés par le SERVEUR (les arguments du client ne sont que des
# valeurs substituées — cf. sécurité §3). Un placeholder par template.
_ANALYZE_SENTIMENT_TEMPLATE = "Analyse le sentiment de ce texte: {text}"
_PLAN_TRAINING_TEMPLATE = "Planifie un entraînement pour: {dataset}"

# Catalogue statique (métadonnées ``prompts/list``) — tuple immuable, ordre
# déterministe : la liste est de la MÉTADONNÉE pure (aucune I/O).
_PROMPTS: tuple[MCPPromptTemplate, ...] = (
    MCPPromptTemplate(
        name=PROMPT_ANALYZE_SENTIMENT,
        description=(
            "Analyse le sentiment d'un texte brut (FR/EN) — template prêt pour "
            "le modèle de classification ThinkTuning."
        ),
        arguments=(
            MCPPromptArgument(
                name="text",
                description="Texte à analyser (français ou anglais).",
                required=True,
            ),
        ),
    ),
    MCPPromptTemplate(
        name=PROMPT_PLAN_TRAINING,
        description=(
            "Planifie un entraînement (dataset, hyperparamètres, versionnement) "
            "pour un dataset de la sandbox ThinkTuning."
        ),
        arguments=(
            MCPPromptArgument(
                name="dataset",
                description="Chemin du dataset sous la sandbox (ex. data/train.csv).",
                required=True,
            ),
        ),
    ),
)

# Index nom → prompt (validation des arguments) — dérivé du même catalogue.
_PROMPT_INDEX: dict[str, MCPPromptTemplate] = {
    prompt.name: prompt for prompt in _PROMPTS
}

# Index nom → template (résolution ``prompts/get``) — une seule source de
# vérité avec le catalogue : aucun drift possible entre list et get.
_TEMPLATES: dict[str, str] = {
    PROMPT_ANALYZE_SENTIMENT: _ANALYZE_SENTIMENT_TEMPLATE,
    PROMPT_PLAN_TRAINING: _PLAN_TRAINING_TEMPLATE,
}


class PromptProvider(MCPPromptRegistryPort):
    """``MCPPromptRegistryPort`` — les 2 prompts ThinkTuning (tâche 9).

    Stateless et thread-safe : le catalogue est immuable (tuples), la
    résolution est pure (aucune I/O) — une instance partagée suffit.
    """

    def list_prompts(self) -> list[MCPPromptTemplate]:
        """Les 2 prompts du catalogue (``prompts/list``) — métadonnée pure."""
        return list(_PROMPTS)

    def get_prompt(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> list[MCPPromptMessage]:
        """Résout un prompt en messages (``prompts/get``).

        Args:
            name: nom du prompt (``analyze-sentiment`` | ``plan-training``) ;
            arguments: valeurs des arguments (chaînes) ; ``None`` → aucun.

        Returns:
            Un unique message ``user`` portant le template résolu.

        Raises:
            NotFoundError: nom inconnu (fail-closed, message actionnable) ;
            ValidationError: argument requis manquant ou valeur non-string.
        """
        prompt = _PROMPT_INDEX.get(name)
        if prompt is None:
            available = ", ".join(sorted(_PROMPT_INDEX))
            raise NotFoundError(
                f"Prompt inconnu : '{name}'. Prompts disponibles : {available}."
            )
        values = dict(arguments or {})
        for key, value in values.items():
            if not isinstance(value, str):
                raise ValidationError(
                    f"Argument invalide : '{key}' doit être une chaîne "
                    f"(reçu {type(value).__name__})."
                )
        missing = [
            argument.name
            for argument in prompt.arguments
            if argument.required and argument.name not in values
        ]
        if missing:
            raise ValidationError(
                f"Argument(s) requis manquant(s) pour le prompt '{name}' : "
                f"{', '.join(missing)}."
            )
        resolved = _TEMPLATES[name].format(**values)
        logger.debug("MCP prompt '%s' résolu (%d caractère(s))", name, len(resolved))
        return [MCPPromptMessage(role="user", content=resolved)]


def build_prompt_provider() -> PromptProvider:
    """Provider par défaut des 2 prompts ThinkTuning (tâche 9).

    Construction SANS I/O ni import lourd : le catalogue est statique.
    """
    return PromptProvider()
