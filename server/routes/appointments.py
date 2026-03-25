from fastapi import APIRouter, Depends, Query, Body

from server.auth import verify_api_key
from server import ema_service
from server.schemas import CreateAppointmentRequest

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
async def create_appointment(body: CreateAppointmentRequest):
    return await ema_service.create_appointment(
        patient_id=body.patient_id,
        provider_id=body.provider_id,
        facility_id=body.facility_id,
        appointment_type_id=body.appointment_type_id,
        scheduled_start=body.scheduled_start,
        duration=body.duration,
        reason=body.reason,
        notes=body.notes,
        new_patient=body.new_patient,
    )


@router.put("/{appointment_id}")
async def update_appointment(appointment_id: str, payload: dict = Body(...)):
    return await ema_service.update_appointment(appointment_id, payload)
