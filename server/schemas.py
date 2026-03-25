from pydantic import BaseModel, Field


class PortalEmailRequest(BaseModel):
    username: str
    email: str


class CreateAppointmentRequest(BaseModel):
    patient_id: int = Field(..., description="EMA patient ID")
    provider_id: int = Field(..., description="EMA provider ID")
    facility_id: int = Field(2040, description="EMA facility ID (default: Liora Derm)")
    appointment_type_id: int = Field(..., description="EMA appointment type ID (from /reference/appointment-types)")
    scheduled_start: str = Field(..., description="Start time in ISO 8601 UTC (e.g. 2026-03-25T14:00:00.000Z)")
    duration: int = Field(15, description="Duration in minutes")
    reason: str = Field("", description="Reason for visit (free text)")
    notes: str = Field("", description="Appointment notes (free text)")
    new_patient: bool = Field(False, description="Whether this is a new patient visit")


class RescheduleRequest(BaseModel):
    new_start: str
    new_duration: int | None = None
    provider_id: int | None = None
    reason: str = "PATIENT_RESCHEDULE"


class CancelRequest(BaseModel):
    reason: str = "PATIENT_CANCELLED"
    notes: str = ""
