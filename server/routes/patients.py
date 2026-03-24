from fastapi import APIRouter, Depends, Query

from server.auth import verify_api_key
from server import ema_service
from server.schemas import PortalEmailRequest

router = APIRouter(prefix="/patients", tags=["patients"], dependencies=[Depends(verify_api_key)])


@router.get("/search")
async def search_patients(
    last_name: str = Query(None),
    first_name: str = Query(None),
    page_size: int = Query(25, le=100),
):
    return await ema_service.search_patients(
        last_name=last_name, first_name=first_name, page_size=page_size,
    )


@router.get("/{patient_id}")
async def get_patient(patient_id: str, selector: str = Query(None)):
    return await ema_service.get_patient(patient_id, selector=selector)


@router.post("/{patient_id}/portal-email", status_code=204)
async def send_portal_email(patient_id: str, body: PortalEmailRequest):
    await ema_service.send_portal_email(patient_id, body.username, body.email)
