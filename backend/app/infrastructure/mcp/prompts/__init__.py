# project/app/infrastructure/mcp/prompts/__init__.py
"""Prompts MCP ThinkTuning (tâche 9 — S3, v1.0.0 Beta).

- ``prompt_provider`` : ``PromptProvider`` (port ``MCPPromptRegistryPort``,
  tâche 3) — 2 prompts résolus LOCALEMENT (aucune I/O, aucun tool, aucun
  LLM) : ``analyze-sentiment`` (argument ``text``) et ``plan-training``
  (argument ``dataset``), arguments requis. Sécurité fail-closed : nom
  inconnu → ``NotFoundError``, argument requis manquant / valeur non-string
  → ``ValidationError`` ; templates possédés par le SERVEUR (les arguments
  du client ne sont que des VALEURS substituées).
"""

from __future__ import annotations

from app.infrastructure.mcp.prompts.prompt_provider import (
    PROMPT_ANALYZE_SENTIMENT,
    PROMPT_PLAN_TRAINING,
    PromptProvider,
    build_prompt_provider,
)

__all__ = [
    "PROMPT_ANALYZE_SENTIMENT",
    "PROMPT_PLAN_TRAINING",
    "PromptProvider",
    "build_prompt_provider",
]
