# project/app/api/routes/maintenance.py

from fastapi import APIRouter, Depends

from app.api.dependencies.auth import require_api_key
from app.api.middlewares.maintenance import is_maintenance_mode, set_maintenance_mode

router = APIRouter(prefix="/maintenance", tags=["Maintenance"])


@router.get("")
def get_status(_: bool = Depends(require_api_key)):
    return {"maintenance_mode": is_maintenance_mode()}


@router.post("/enable")
def enable(_: bool = Depends(require_api_key)):
    set_maintenance_mode(True)
    return {"status": "enabled"}


@router.post("/disable")
def disable(_: bool = Depends(require_api_key)):
    set_maintenance_mode(False)
    return {"status": "disabled"}
