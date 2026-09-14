# project/app/api/dependencies/mcp_first.py

"""Garde « lecture seule » du mode MCP-First — partagé par les surfaces agent.

Décision S7 (tâche 20, ``docs/mcp/IMPLEMENTATION_PLAN.md``) : quand le réglage
``MCP_FIRST=true`` est actif (persisté côté IHM, env en repli), la surface HTTP
de l'agent passe en LECTURE SEULE — tout endpoint mutant répond 405
``mcp_first_read_only`` et renvoie vers MCP (``POST /mcp/sse``).

Le garde vit dans ``api/dependencies/`` (et non dans une route) parce que les
DEUX surfaces le partagent : l'adaptateur legacy (``routes/agent.py``) et la
surface versionnée (``routes/v1/agent.py``). L'invariant verrouillé par
``tests/test_mcp_first.py`` est que les routes v1 héritent du MÊME refus, la
règle de policy ne devant exister qu'une fois.

Exception documentée : l'approbation humaine (``/approvals/{id}/approve`` et
``/reject``) n'est PAS décorée — c'est le canal qui débloque les runs MCP en
``pending_approval`` ; le bloquer interdirait à tout run à risque de se
terminer.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, TypeVar, cast

from fastapi import HTTPException

from app.agent.settings import get_agent_config

_F = TypeVar("_F", bound=Callable[..., Any])

#: Code d'erreur stable renvoyé aux clients (contrat S7).
READ_ONLY_CODE = "mcp_first_read_only"

#: Message utilisateur — référence explicite à la surface de remplacement.
READ_ONLY_MESSAGE = (
    "API HTTP legacy en lecture seule (MCP_FIRST=true) : "
    "les mutations passent par MCP (POST /mcp/sse)."
)


def mcp_first_read_only() -> bool:
    """Vrai si la surface HTTP de l'agent est gelée en lecture seule.

    Lecture à l'appel (aucun cache) : ``monkeypatch.setenv("MCP_FIRST", ...)``
    suffit à basculer le comportement dans les tests.
    """
    return get_agent_config().mcp_first


def writable_endpoint(func: _F) -> _F:
    """Refuse un endpoint MUTANT quand ``MCP_FIRST=true`` (405 + renvoi MCP).

    Le refus intervient AVANT toute exécution (aucun effet de bord) et porte
    le corps ``{"detail": {"code", "message"}}`` attendu par le dashboard.
    """

    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if mcp_first_read_only():
            raise HTTPException(
                status_code=405,
                detail={"code": READ_ONLY_CODE, "message": READ_ONLY_MESSAGE},
            )
        return func(*args, **kwargs)

    return cast(_F, wrapper)
