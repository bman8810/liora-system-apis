from fastapi import Security, HTTPException
from fastapi.security import APIKeyHeader

from server.config import settings

_header = APIKeyHeader(name="X-API-Key")


async def verify_api_key(key: str = Security(_header)):
    if key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return key
