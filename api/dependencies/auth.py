# project/api/dependencies/auth.py

import os
from fastapi import Header, HTTPException

API_KEY = os.getenv("API_KEY") or "dev-local-api-key"


def require_api_key(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> bool:
    expected_key = os.getenv("API_KEY") or API_KEY or "dev-local-api-key"
    if x_api_key != expected_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header.")
    return True

def _get_api_key() -> str:
    return os.getenv("API_KEY") or API_KEY or "dev-local-api-key"