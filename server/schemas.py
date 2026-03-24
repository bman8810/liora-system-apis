from pydantic import BaseModel


class PortalEmailRequest(BaseModel):
    username: str
    email: str


class RescheduleRequest(BaseModel):
    new_start: str
    new_duration: int | None = None
    provider_id: int | None = None
    reason: str = "PATIENT_RESCHEDULE"


class CancelRequest(BaseModel):
    reason: str = "PATIENT_CANCELLED"
    notes: str = ""
