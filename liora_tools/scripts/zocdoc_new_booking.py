"""Zocdoc new patient booking processor.

Scans recent Zocdoc bookings for new patients, sends call requests and
welcome SMS messages, and reports each step to Genies Bottle.

Usage:
    python -m liora_tools.scripts.zocdoc_new_booking [--dry-run]
"""

import os
import sys
import time
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv

load_dotenv()

SMS_TEMPLATE_ID = "00914ffc-ae68-49c8-a76d-a0d78a5d5d21"
SMS_FALLBACK_BODY = (
    "Hi {{FIRST_NAME}}, thank you for booking with Liora Dermatology & Aesthetics! "
    "We look forward to seeing you. Please call (212) 433-4569 if you have any questions "
    "or need to make changes to your appointment."
)

# Flow definition for self-registration
FLOW_DEFINITION = {
    "filters": [
        {"name": "recent_bookings", "description": "bookingTimeUtc <= 60m ago"},
        {"name": "new_patients", "description": "patientType == NEW"},
        {"name": "not_cancelled", "description": "status != PATIENT_CANCELLED"},
    ],
    "gates": [
        {"name": "already_processed", "service": "genies-bottle", "description": "Skip if zocdoc-new-booking execution exists for this MRN"},
        {"name": "already_contacted", "service": "weave", "description": "Skip if Weave messages found for patient name"},
    ],
    "steps": [
        {"name": "get_booking_details", "service": "zocdoc", "description": "Fetch full booking with PHI"},
        {"name": "send_call_request", "service": "zocdoc", "description": "Send 'call the office' request", "llm_fallback": False},
        {"name": "send_welcome_sms", "service": "weave", "description": "Send welcome SMS to patient", "llm_fallback": False},
        {"name": "send_portal_email", "service": "ema", "description": "Send patient portal access email", "llm_fallback": False},
        {"name": "report_completed", "service": "genies-bottle", "description": "Report execution completed", "llm_fallback": False},
    ],
}


WEAVE_TOKEN_FILE = os.path.expanduser("~/.liora/weave_token.txt")


def _refresh_weave_token():
    """Read fresh Weave JWT from the token drop file.

    A separate Claude Code cron job deposits a fresh token into
    ~/.liora/weave_token.txt every 20 minutes via Claude-in-Chrome.

    Returns a valid token string, or None if not available.
    """
    import base64
    import json as _json

    # Check if current env token is still valid
    current = os.environ.get("WEAVE_TOKEN", "")
    if current:
        try:
            payload = current.split(".")[1]
            payload += "=" * (4 - len(payload) % 4)
            d = _json.loads(base64.b64decode(payload))
            remaining = d["exp"] - datetime.now(timezone.utc).timestamp()
            if remaining > 300:  # more than 5 min left
                return None  # current token is fine
        except Exception:
            pass  # can't parse, try to refresh

    # Read from drop file
    if not os.path.exists(WEAVE_TOKEN_FILE):
        return None

    with open(WEAVE_TOKEN_FILE) as f:
        token = f.read().strip()

    if not (token.startswith("eyJ") and token.count(".") == 2):
        return None

    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        d = _json.loads(base64.b64decode(payload))
        remaining = d["exp"] - datetime.now(timezone.utc).timestamp()
        if remaining > 60:
            return token
    except Exception:
        pass

    return None


def _init_clients():
    """Initialize all API clients."""
    from liora_tools.zocdoc.client import ZocdocClient
    from liora_tools.weave.client import WeaveClient
    from liora_tools.genies_bottle.client import GenieBottleClient

    from liora_tools.modmed.client import EmaClient

    zoc = ZocdocClient.from_profile()

    # Try refreshing the Weave token from Chrome first
    fresh_token = _refresh_weave_token()
    if fresh_token:
        os.environ["WEAVE_TOKEN"] = fresh_token
    weave = WeaveClient.connect()  # tests token, falls back to browser login if expired

    ema = EmaClient.connect()
    gb = GenieBottleClient.from_api_key()

    # Fetch SMS template from Weave (fall back to hardcoded if unavailable)
    sms_template = _fetch_sms_template(weave)

    return zoc, weave, ema, gb, sms_template


def _fetch_sms_template(weave):
    """Fetch the SMS template body from Weave's template API."""
    from liora_tools.config import WeaveConfig
    cfg = WeaveConfig()
    try:
        r = weave._s.get(
            f"{cfg.api_base}/messaging/templator/v2/templates",
            params={"orgId": cfg.tenant_id},
        )
        if not r.ok:
            return SMS_FALLBACK_BODY

        templates = r.json()
        if isinstance(templates, dict):
            templates = templates.get("templates", [])

        for t in templates:
            if t.get("templateId") == SMS_TEMPLATE_ID:
                return t.get("templateString", SMS_FALLBACK_BODY)

    except Exception:
        pass

    return SMS_FALLBACK_BODY


def _extract_candidates(bookings_data, lookback_minutes=60):
    """Filter bookings to new patient candidates within the lookback window."""
    appointments = (
        bookings_data.get("data", {})
        .get("appointments", {})
        .get("appointments", [])
    )

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    candidates = []

    for appt in appointments:
        # Filter: only bookings within the last 60 minutes (skip if older)
        booking_time_str = appt.get("bookingTimeUtc")
        if not booking_time_str:
            continue
        try:
            booking_time = datetime.fromisoformat(booking_time_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if booking_time < cutoff:
            continue

        # Filter: patientType == NEW
        if appt.get("patientType") != "NEW":
            continue

        # Filter: status != PATIENT_CANCELLED
        if appt.get("appointmentStatus") == "PATIENT_CANCELLED":
            continue

        candidates.append(appt)

    return candidates


def _build_correlation_id(mrn, appt_date):
    """Build a deterministic correlation ID for upsert."""
    return f"zocdoc-{mrn}-{appt_date}"


def main(dry_run=False, lookback_minutes=90):
    start_time = time.time()
    zoc, weave, ema, gb, sms_template = _init_clients()

    if not dry_run:
        # Register flow definition on startup
        try:
            gb._post("/api/webhooks/register-flow", {
                "task_slug": "zocdoc-new-booking",
                "flow_definition": FLOW_DEFINITION,
                "schedule": {"cron": "*/30 * * * *", "timezone": "America/New_York"},
            })
        except Exception:
            pass  # best-effort self-registration

        # Log scan start
        gb.log_activity(
            "zocdoc_routine_check",
            "Starting Zocdoc new patient scan",
            source="zocdoc-cron",
        )
    else:
        print("DRY-RUN mode — no messages, calls, or GB reports will be sent\n")

    # Fetch bookings
    bookings_data = zoc.list_bookings()
    candidates = _extract_candidates(bookings_data, lookback_minutes=lookback_minutes)

    print(f"Found {len(candidates)} new patient candidate(s)")

    processed = 0
    skipped = 0
    errors = 0

    for appt in candidates:
        appointment_id = appt.get("appointmentId", "")
        patient_obj = appt.get("patient", {})
        first_name = patient_obj.get("firstName", "")
        last_name = patient_obj.get("lastName", "")
        patient_name = f"{first_name} {last_name}".strip() or "(unknown)"
        mrn = appt.get("mrn") or appt.get("patientId") or appointment_id
        appt_date = (appt.get("appointmentTimeUtc") or datetime.now(timezone.utc).strftime("%Y-%m-%d"))[:10]
        correlation_id = _build_correlation_id(mrn, appt_date)

        try:
            # GATE 1: Already processed?
            prior = gb.query_executions(
                task_slug="zocdoc-new-booking",
                correlation_id=correlation_id,
                status="completed",
            )
            if prior:
                print(f"  SKIP {patient_name} (MRN {mrn}): already processed")
                skipped += 1
                continue

            # GATE 2: Already contacted via Weave?
            search_query = f"{first_name} {last_name}".strip()
            if search_query:
                weave_results = weave.search_messages(search_query)
                if int(weave_results.get("numResults", 0)) > 0:
                    print(f"  SKIP {patient_name}: existing Weave messages found")
                    skipped += 1
                    continue

            # Step 1: Get full booking details (runs in both live and dry-run)
            booking_detail = zoc.get_booking(appointment_id)
            appt_details = (
                booking_detail.get("data", {})
                .get("appointmentDetails", {})
            )
            patient_info = appt_details.get("patient", {})
            phone = patient_info.get("phoneNumber") or ""
            request_id = appt_details.get("requestId") or appt.get("requestId")
            actual_first = patient_info.get("firstName") or first_name

            # Look up patient in EMA for portal email
            email = patient_info.get("email") or ""
            ema_patient_id = None
            if last_name:
                try:
                    ema_results = ema.search_patients(last_name=last_name, first_name=first_name)
                    if ema_results:
                        ema_patient_id = ema_results[0].get("id")
                        email = email or ema_results[0].get("email", "")
                except Exception:
                    pass  # EMA lookup is best-effort

            if dry_run:
                sms_body = sms_template.replace("{{FIRST_NAME}}", actual_first)
                print(f"  DRY-RUN {patient_name} (MRN {mrn}):")
                print(f"    correlation_id: {correlation_id}")
                print(f"    phone: {phone or '(none)'}")
                print(f"    email: {email or '(none)'}")
                print(f"    requestId: {request_id or '(none)'}")
                print(f"    ema_patient_id: {ema_patient_id or '(none)'}")
                print(f"    WOULD send_call_request({request_id}, reasons=['Other'])" if request_id else "    WOULD skip call request (no requestId)")
                print(f"    WOULD send_message({phone}, ...)" if phone else "    WOULD skip SMS (no phone)")
                print(f"    WOULD send_portal_email({ema_patient_id}, {email})" if ema_patient_id and email else f"    WOULD skip portal email (ema_id={ema_patient_id}, email={email or 'none'})")
                print(f"    SMS body: {sms_body}")
                skipped += 1
                continue

            # Report running
            gb.report_process(
                "zocdoc-new-booking",
                "running",
                correlation_id=correlation_id,
                trigger_type="cron",
                trigger_source="zocdoc",
                patient={"mrn": mrn, "name": patient_name, "phone": phone},
                appointment={"id": appointment_id, "date": appt_date},
            )

            steps = []

            # Step 2: Send call request
            if request_id:
                zoc.send_call_request(str(request_id), reasons=["Other"])
                steps.append({
                    "name": "send_call_request",
                    "status": "done",
                    "detail": f"Call request sent for requestId {request_id}",
                })
            else:
                steps.append({
                    "name": "send_call_request",
                    "status": "skipped",
                    "detail": "No requestId available",
                })

            # Step 3: Send welcome SMS
            if phone:
                sms_body = sms_template.replace("{{FIRST_NAME}}", actual_first)
                weave.send_message(phone, sms_body)
                steps.append({
                    "name": "send_welcome_sms",
                    "status": "done",
                    "detail": f"SMS sent to {phone}",
                })
            else:
                steps.append({
                    "name": "send_welcome_sms",
                    "status": "skipped",
                    "detail": "No phone number available",
                })

            # Step 4: Send patient portal email
            if ema_patient_id and email:
                try:
                    ema.send_portal_email(ema_patient_id, email, email)
                    steps.append({
                        "name": "send_portal_email",
                        "status": "done",
                        "detail": f"Portal email sent to {email}",
                    })
                except Exception as portal_err:
                    steps.append({
                        "name": "send_portal_email",
                        "status": "error",
                        "detail": str(portal_err),
                    })
            else:
                steps.append({
                    "name": "send_portal_email",
                    "status": "skipped",
                    "detail": f"Missing ema_patient_id={ema_patient_id} or email={email or 'none'}",
                })

            # Step 5: Report completed
            elapsed = int((time.time() - start_time) * 1000)
            gb.report_process(
                "zocdoc-new-booking",
                "completed",
                correlation_id=correlation_id,
                steps=steps,
                outcome_summary=f"Processed new patient {patient_name}",
                duration_ms=elapsed,
            )
            gb.log_activity(
                "zocdoc_new_patient_processed",
                f"Processed new ZocDoc patient: {patient_name}",
                source="zocdoc-cron",
                patient={"mrn": mrn, "name": patient_name},
            )

            print(f"  OK {patient_name} (MRN {mrn}): processed")
            processed += 1

        except Exception as e:
            errors += 1
            print(f"  ERROR {patient_name} (MRN {mrn}): {e}", file=sys.stderr)

            try:
                gb.report_process(
                    "zocdoc-new-booking",
                    "failed",
                    correlation_id=correlation_id,
                    error_message=str(e),
                )
                gb.request_feedback(
                    f"Zocdoc new booking failed: {patient_name}",
                    description=f"Error processing {patient_name} (MRN {mrn}): {e}",
                    priority="high",
                    patient={"mrn": mrn, "name": patient_name},
                )
            except Exception:
                pass  # reporting failures are best-effort

            continue

    # Summary
    elapsed_s = round(time.time() - start_time, 1)
    summary = f"Scan complete: {processed} processed, {skipped} skipped, {errors} errors ({elapsed_s}s)"
    print(summary)

    try:
        zoc.close()
    except Exception:
        pass


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    lookback = 90
    for arg in sys.argv:
        if arg.startswith("--lookback-minutes="):
            lookback = int(arg.split("=", 1)[1])
    main(dry_run=dry, lookback_minutes=lookback)
