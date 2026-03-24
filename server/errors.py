from fastapi import Request
from fastapi.responses import JSONResponse

from liora_tools.exceptions import (
    AuthenticationError,
    LioraAPIError,
    OptimisticLockError,
    RateLimitError,
    SafetyGuardError,
)


async def authentication_error_handler(request: Request, exc: AuthenticationError):
    from server.ema_service import clear_client
    clear_client()
    return JSONResponse(
        status_code=503,
        content={"error": "ema_session_expired", "detail": str(exc)},
        headers={"Retry-After": "30"},
    )


async def rate_limit_handler(request: Request, exc: RateLimitError):
    return JSONResponse(
        status_code=429,
        content={"error": "ema_rate_limited", "detail": str(exc)},
        headers={"Retry-After": "10"},
    )


async def optimistic_lock_handler(request: Request, exc: OptimisticLockError):
    return JSONResponse(
        status_code=409,
        content={"error": "concurrent_modification", "detail": str(exc)},
    )


async def safety_guard_handler(request: Request, exc: SafetyGuardError):
    return JSONResponse(
        status_code=403,
        content={"error": "safety_guard", "detail": str(exc)},
    )


async def liora_api_error_handler(request: Request, exc: LioraAPIError):
    return JSONResponse(
        status_code=502,
        content={
            "error": "ema_api_error",
            "detail": str(exc),
            "upstream_status": exc.status_code,
        },
    )


def register_error_handlers(app):
    app.add_exception_handler(AuthenticationError, authentication_error_handler)
    app.add_exception_handler(RateLimitError, rate_limit_handler)
    app.add_exception_handler(OptimisticLockError, optimistic_lock_handler)
    app.add_exception_handler(SafetyGuardError, safety_guard_handler)
    app.add_exception_handler(LioraAPIError, liora_api_error_handler)
