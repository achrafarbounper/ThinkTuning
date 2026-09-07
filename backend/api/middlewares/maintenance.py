# project/api/middlewares/maintenance.py

import json
import threading

from fastapi import Request
from fastapi.responses import Response

_MAINTENANCE_MODE = False
_MAINTENANCE_LOCK = threading.Lock()
_MAINTENANCE_MESSAGE = "Service under maintenance. Please try again later."


def is_maintenance_mode() -> bool:
    with _MAINTENANCE_LOCK:
        return _MAINTENANCE_MODE


def set_maintenance_mode(enabled: bool, message: str | None = None):
    global _MAINTENANCE_MODE, _MAINTENANCE_MESSAGE
    with _MAINTENANCE_LOCK:
        _MAINTENANCE_MODE = enabled
        if message:
            _MAINTENANCE_MESSAGE = message


async def maintenance_mode_middleware(request: Request, call_next):
    # /api/v1/health* : équivalent versionné de /health — un healthcheck doit
    # rester répondant PENDANT la maintenance (c'est lui qui signale l'état).
    excluded_paths = {
        "/health",
        "/api/v1/health",
        "/api/v1/health/model-sanity",
        "/maintenance",
        "/maintenance/enable",
        "/maintenance/disable",
        "/metrics",
    }
    if request.url.path not in excluded_paths and is_maintenance_mode():
        return Response(
            content=json.dumps({"detail": _MAINTENANCE_MESSAGE}),
            status_code=503,
            media_type="application/json",
            headers={"Retry-After": "3600"},
        )
    return await call_next(request)
