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


# -- Read-only scheduling flow --

@router.get("/flow/validate")
async def flow_validate(
    last_name: str = Query(None),
    first_name: str = Query(None),
    dob: str = Query(None, description="YYYY-MM-DD"),
    phone: str = Query(None),
    mrn: str = Query(None),
    include_test_patients: bool = Query(
        False,
        description="Keep TEST/PHREESIA charts (lab). Default filters them on multi-match.",
    ),
):
    return await ema_service.validate_patient(
        last_name=last_name, first_name=first_name, dob=dob,
        phone=phone, mrn=mrn, include_test_patients=include_test_patients,
    )


@router.get("/flow/patients/{patient_id}/upcoming")
async def flow_upcoming(
    patient_id: str,
    days_ahead: int = Query(90),
):
    return await ema_service.list_upcoming_for_patient(
        patient_id=patient_id, days_ahead=days_ahead,
    )


@router.get("/flow/patients/{patient_id}/past")
async def flow_past(
    patient_id: str,
    days_back: int = Query(365),
    limit: int = Query(5),
    include_cancelled: bool = Query(False),
):
    return await ema_service.list_past_for_patient(
        patient_id=patient_id,
        days_back=days_back,
        limit=limit,
        include_cancelled=include_cancelled,
    )


@router.get("/flow/lookup")
async def flow_lookup(
    last_name: str = Query(None),
    first_name: str = Query(None),
    dob: str = Query(None, description="YYYY-MM-DD"),
    phone: str = Query(None),
    mrn: str = Query(None),
    days_ahead: int = Query(90),
    days_back: int = Query(365),
    include_past: bool = Query(True),
    include_test_patients: bool = Query(False),
    appt_type_id: str = Query(None),
    duration: int = Query(15),
    time_of_day: str = Query("ANYTIME"),
    specific_date: str = Query(None, description="YYYY-MM-DD"),
    slot_limit: int = Query(5),
):
    return await ema_service.scheduling_lookup(
        last_name=last_name, first_name=first_name, dob=dob,
        phone=phone, mrn=mrn, days_ahead=days_ahead,
        days_back=days_back, include_past=include_past,
        include_test_patients=include_test_patients,
        appt_type_id=appt_type_id, duration=duration,
        time_of_day=time_of_day, specific_date=specific_date,
        slot_limit=slot_limit,
    )


@router.get("/flow/visit-types")
async def flow_visit_types():
    return await ema_service.list_visit_types()
