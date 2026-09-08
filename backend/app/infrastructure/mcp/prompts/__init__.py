# project/app/infrastructure/mcp/prompts/__init__.py
"""Prompts MCP ThinkTuning (tâches 9 — S3, v1.0.0 Beta — + 14 — S5, v1.1.0).

- ``prompt_provider`` : ``PromptProvider`` (port ``MCPPromptRegistryPort``,
  tâche 3) — 5 prompts résolus LOCALEMENT (aucune I/O, aucun tool, aucun
  LLM) : ``analyze-sentiment`` (argument ``text``), ``plan-training``
  (argument ``dataset``), ``summarize-job`` (argument ``job_id``),
  ``compare-models`` (arguments ``v1``, ``v2``) et ``explain-prediction``
  (argument ``text``), arguments requis. Sécurité fail-closed : nom
  inconnu → ``NotFoundError``, argument requis manquant / valeur non-string
  → ``ValidationError`` ; templates possédés par le SERVEUR (les arguments
  du client ne sont que des VALEURS substituées).
"""

from __future__ import annotations

from app.infrastructure.mcp.prompts.prompt_provider import (
    PROMPT_ANALYZE_SENTIMENT,
    PROMPT_COMPARE_MODELS,
    PROMPT_EXPLAIN_PREDICTION,
    PROMPT_PLAN_TRAINING,
    PROMPT_SUMMARIZE_JOB,
    PromptProvider,
    build_prompt_provider,
)

__all__ = [
    "PROMPT_ANALYZE_SENTIMENT",
    "PROMPT_COMPARE_MODELS",
    "PROMPT_EXPLAIN_PREDICTION",
    "PROMPT_PLAN_TRAINING",
    "PROMPT_SUMMARIZE_JOB",
    "PromptProvider",
    "build_prompt_provider",
]
