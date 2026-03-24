from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware

from server.auth import verify_api_key
from server.config import settings
from server.errors import register_error_handlers
from server.routes import patients, appointments, scheduling, reference
from server import ema_service

app = FastAPI(title="Liora EMA API", version="0.1.0")

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Error handlers
register_error_handlers(app)

# Routes
prefix = "/api/v1/ema"
app.include_router(patients.router, prefix=prefix)
app.include_router(appointments.router, prefix=prefix)
app.include_router(scheduling.router, prefix=prefix)
app.include_router(reference.router, prefix=prefix)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/health/ema", dependencies=[Depends(verify_api_key)])
async def health_ema():
    alive = await ema_service.check_session()
    return {"ema_session": "valid" if alive else "expired"}
