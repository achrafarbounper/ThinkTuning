# project/app/infrastructure/mcp/sampling/__init__.py
"""Sampling MCP (reverse LLM inference) — tache 15 (S6, v2.0.0).

- ``SamplingAdapter`` : ``SamplingPort`` via ``LLMClientPort`` existant
  (``HttpLLMClient`` en prod, ``StubLLMClient`` en tests) ;
- ``build_sampling_adapter`` : fabrique (injection du client LLM).
"""

from __future__ import annotations

from app.infrastructure.mcp.sampling.sampling_adapter import (
    SamplingAdapter,
    build_sampling_adapter,
)

__all__ = ["SamplingAdapter", "build_sampling_adapter"]
