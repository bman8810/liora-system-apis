from fastapi import APIRouter, Depends, Query, Body

from server.auth import verify_api_key
from server import ema_service

router = APIRouter(prefix="/appointments", tags=["appointments"], dependencies=[Depends(verify_api_key)])


@router.get("")
async def list_appointments(
    start_date: str = Query(None, description="YYYY-MM-DD"),
    end_date: str = Query(None, description="YYYY-MM-DD"),
    page_size: int = Query(50, le=200),
):
    return await ema_service.list_appointments(
        start_date=start_date, end_date=end_date, page_size=page_size,
    )


@router.get("/{appointment_id}")
async def get_appointment(appointment_id: str, selector: str = Query(None)):
    return await ema_service.get_appointment(appointment_id, selector=selector)


@router.post("", status_code=201)
async def create_appointment(payload: dict = Body(...)):
    return await ema_service.create_appointment(payload)


@router.put("/{appointment_id}")
async def update_appointment(appointment_id: str, payload: dict = Body(...)):
    return await ema_service.update_appointment(appointment_id, payload)
