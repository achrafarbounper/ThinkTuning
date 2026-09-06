# project/api/routes/v1/__init__.py
"""Router racine de l'API versionnée v1 (montage : prefix="/api/v1").

Surface DE COUPLAGE FRONTEND/BACKEND : les endpoints y sont stables, les
breaking changes passeront par une future v2 (jamais par mutation de v1).
Les routes legacy (sans préfixe) restent servies en parallèle pendant la
migration (strangler pattern) — la bascule du dashboard se fera endpoint
par endpoint.
"""

from fastapi import APIRouter

from . import (
    agent,
    chat,
    evaluate,
    health,
    intent_training,
    models,
    prediction,
    sessions,
    training,
)

router = APIRouter()
router.include_router(health.router)
router.include_router(prediction.router)
router.include_router(training.router)
router.include_router(intent_training.router)
router.include_router(models.router)
router.include_router(evaluate.router)
router.include_router(agent.router)
router.include_router(sessions.router)
router.include_router(chat.router)

__all__ = ["router"]
