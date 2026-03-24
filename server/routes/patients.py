from fastapi import APIRouter, Depends, Query

from server.auth import verify_api_key
from server import ema_service
from server.schemas import PortalEmailRequest

router = APIRouter(prefix="/patients", tags=["patients"], dependencies=[Depends(verify_api_key)])


@router.get("/search")
async def search_patients(
    last_name: str = Query(None, description="Patient last name"),
    first_name: str = Query(None, description="Patient first name"),
    phone: str = Query(None, description="Phone number (any format, digits extracted). Use with name or DOB for best results."),
    dob: str = Query(None, description="Date of birth (YYYY-MM-DD)"),
    mrn: str = Query(None, description="Medical record number"),
    page_size: int = Query(25, le=100),
):
    return await ema_service.search_patients(
        last_name=last_name, first_name=first_name,
        phone=phone, dob=dob, mrn=mrn, page_size=page_size,
    )


@router.get("/{patient_id}")
async def get_patient(patient_id: str, selector: str = Query(None)):
    return await ema_service.get_patient(patient_id, selector=selector)


@router.get("/{patient_id}/appointments")
async def get_patient_appointments(
    patient_id: str,
    start_date: str = Query(None, description="YYYY-MM-DD"),
    end_date: str = Query(None, description="YYYY-MM-DD"),
    page_size: int = Query(50, le=200),
):
    return await ema_service.get_patient_appointments(
        patient_id=patient_id, start_date=start_date,
        end_date=end_date, page_size=page_size,
    )


@router.post("/{patient_id}/portal-email", status_code=204)
async def send_portal_email(patient_id: str, body: PortalEmailRequest):
    await ema_service.send_portal_email(patient_id, body.username, body.email)
