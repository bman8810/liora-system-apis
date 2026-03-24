from fastapi import APIRouter, Depends, Query

from server.auth import verify_api_key
from server import ema_service
from server.schemas import RescheduleRequest, CancelRequest

router = APIRouter(prefix="/scheduling", tags=["scheduling"], dependencies=[Depends(verify_api_key)])


@router.get("/slots")
async def find_slots(
    appt_type_id: str = Query(...),
    duration: int = Query(15),
    time_of_day: str = Query("ANYTIME"),
    specific_date: str = Query(None, description="YYYY-MM-DD"),
    time_frame: str = Query("FIRST_AVAILABLE"),
    display: str = Query("BY_PROVIDER"),
):
    return await ema_service.find_slots(
        appt_type_id=appt_type_id, duration=duration,
        time_of_day=time_of_day, specific_date=specific_date,
        time_frame=time_frame, display=display,
    )


@router.post("/reschedule/{appointment_id}")
async def reschedule(appointment_id: str, body: RescheduleRequest):
    return await ema_service.reschedule(
        appointment_id=appointment_id, new_start=body.new_start,
        new_duration=body.new_duration, provider_id=body.provider_id,
        reason=body.reason,
    )


@router.post("/cancel/{appointment_id}")
async def cancel_appointment(appointment_id: str, body: CancelRequest):
    return await ema_service.cancel_appointment(
        appointment_id=appointment_id, reason=body.reason, notes=body.notes,
    )
