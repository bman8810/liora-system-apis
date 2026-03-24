from fastapi import APIRouter, Depends

from server.auth import verify_api_key
from server import ema_service

router = APIRouter(prefix="/reference", tags=["reference"], dependencies=[Depends(verify_api_key)])


@router.get("/appointment-types")
async def list_appointment_types():
    return await ema_service.list_appointment_types()


@router.get("/facilities")
async def list_facilities():
    return await ema_service.list_facilities()


@router.get("/cancel-reasons")
async def list_cancel_reasons():
    return await ema_service.list_cancel_reasons()
