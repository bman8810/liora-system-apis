"""CLI implementation for liora_tools.

Usage:
    python -m liora_tools weave list-threads
    python -m liora_tools ema search-patients --last-name Kim
    python -m liora_tools zocdoc list-bookings --status UNCONFIRMED
    python -m liora_tools auth check

All commands output JSON to stdout. Errors go to stderr as JSON.
Set PORTAL_URL to enable automatic activity reporting to the Genie Portal.
Set AGENT_ID to tag reports with the calling agent's identity.
"""

import argparse
import json
import os
import sys

import requests

from liora_tools.auth.session_manager import check_all, get_client, refresh_platform
from liora_tools.exceptions import LioraAPIError

PORTAL_URL = os.environ.get("PORTAL_URL", "")


def _output(data):
    """Print JSON to stdout."""
    json.dump(data, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")


def _error(message: str, code: int = 1):
    """Print error JSON to stderr and exit."""
    json.dump({"error": message}, sys.stderr, indent=2, default=str)
    sys.stderr.write("\n")
    sys.exit(code)


def _report_activity(agent_id: str, action: str, description: str,
                     source: str, payload: dict = None):
    """Best-effort activity report to the portal (if PORTAL_URL is set)."""
    if not PORTAL_URL:
        return
    try:
        requests.post(
            f"{PORTAL_URL}/api/webhooks/activity",
            json={
                "agent_id": agent_id or "cli",
                "action": action,
                "description": description,
                "source": source,
                "payload": payload or {},
            },
            timeout=5,
        )
    except Exception:
        pass  # reporting is best-effort


# ── Weave Commands ──────────────────────────────────────────────────────────


def weave_list_threads(args):
    client = get_client("weave")
    _output(client.list_threads(page_size=args.page_size))


def weave_get_thread(args):
    client = get_client("weave")
    _output(client.get_thread(args.id, page_size=args.page_size))


def weave_send_message(args):
    client = get_client("weave")
    result = client.send_message(args.phone, args.body, person_id=args.person_id)
    _output(result)
    _report_activity(
        args.agent_id, "weave-api-sms-sent",
        f"Sent SMS to {args.phone} via Weave API",
        "liora_tools.weave",
        {"method": "send_message", "phone": args.phone},
    )


def weave_search_contacts(args):
    client = get_client("weave")
    _output(client.search_persons(args.query, page_size=args.page_size))


def weave_lookup_phone(args):
    client = get_client("weave")
    _output(client.lookup_by_phone(args.phone))


def weave_get_person(args):
    client = get_client("weave")
    _output(client.get_person(args.id))


def weave_list_call_records(args):
    client = get_client("weave")
    _output(client.list_call_records(page_size=args.page_size))


def weave_list_voicemails(args):
    client = get_client("weave")
    _output(client.list_voicemails(page_size=args.page_size))


def weave_dial(args):
    client = get_client("weave")
    result = client.dial(args.phone)
    _output(result)
    _report_activity(
        args.agent_id, "weave-api-call-initiated",
        f"Initiated call to {args.phone} via Weave API",
        "liora_tools.weave",
        {"method": "dial", "phone": args.phone},
    )


# ── EMA Commands ────────────────────────────────────────────────────────────


def ema_search_patients(args):
    client = get_client("ema")
    _output(client.search_patients(
        last_name=args.last_name, first_name=args.first_name,
        page_size=args.page_size,
    ))


def ema_get_patient(args):
    client = get_client("ema")
    _output(client.get_patient(args.id))


def ema_list_appointments(args):
    client = get_client("ema")
    _output(client.list_appointments(
        start_date=args.start_date, end_date=args.end_date,
        page_size=args.page_size,
    ))


def ema_get_appointment(args):
    client = get_client("ema")
    _output(client.get_appointment(args.id))


def ema_find_slots(args):
    client = get_client("ema")
    _output(client.find_slots(
        appt_type_id=args.type_id, duration=args.duration,
        specific_date=args.date,
    ))


def ema_cancel_appointment(args):
    client = get_client("ema")
    result = client.cancel_appointment(
        appointment_id=args.id, reason=args.reason, notes=args.notes or "",
    )
    _output(result)
    _report_activity(
        args.agent_id, "ema-api-appointment-cancelled",
        f"Cancelled appointment {args.id} via EMA API",
        "liora_tools.ema",
        {"method": "cancel_appointment", "appointment_id": args.id, "reason": args.reason},
    )


def ema_reschedule(args):
    client = get_client("ema")
    result = client.reschedule(
        appointment_id=args.id, new_start=args.start,
        new_duration=args.duration,
    )
    _output(result)
    _report_activity(
        args.agent_id, "ema-api-appointment-rescheduled",
        f"Rescheduled appointment {args.id} to {args.start} via EMA API",
        "liora_tools.ema",
        {"method": "reschedule", "appointment_id": args.id, "new_start": args.start},
    )


def ema_list_appointment_types(args):
    client = get_client("ema")
    _output(client.list_appointment_types())


def ema_list_cancel_reasons(args):
    client = get_client("ema")
    _output(client.list_cancel_reasons())


# ── ZocDoc Commands ─────────────────────────────────────────────────────────


def zocdoc_list_bookings(args):
    client = get_client("zocdoc")
    statuses = args.status.split(",") if args.status else None
    _output(client.list_bookings(
        page_number=args.page, page_size=args.page_size,
        statuses=statuses, patient_name=args.patient_name or "",
    ))


def zocdoc_get_booking(args):
    client = get_client("zocdoc")
    _output(client.get_booking(args.id))


def zocdoc_get_status_counts(args):
    client = get_client("zocdoc")
    _output(client.get_status_counts())


def zocdoc_mark_as_read(args):
    client = get_client("zocdoc")
    result = client.mark_as_read(args.id)
    _output(result)
    _report_activity(
        args.agent_id, "zocdoc-api-booking-confirmed",
        f"Marked booking {args.id} as confirmed via ZocDoc API",
        "liora_tools.zocdoc",
        {"method": "mark_as_read", "appointment_id": args.id},
    )


def zocdoc_send_call_request(args):
    # REST endpoint is blocked by DataDome — goes through browser
    from liora_tools.auth.zocdoc import send_call_request_browser
    reasons = args.reasons.split(",") if args.reasons else None
    result = send_call_request_browser(args.id, reasons=reasons)
    _output(result)
    _report_activity(
        args.agent_id, "zocdoc-api-call-request-sent",
        f"Sent call request for booking {args.id} via ZocDoc browser",
        "liora_tools.zocdoc",
        {"method": "send_call_request_browser", "request_id": args.id},
    )


# ── Auth Commands ───────────────────────────────────────────────────────────


def auth_check(args):
    _output(check_all())


def auth_refresh(args):
    _output(refresh_platform(args.target))


# ── CLI Builder ─────────────────────────────────────────────────────────────


def _add_page_size(parser, default=25):
    parser.add_argument("--page-size", type=int, default=default)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="liora_tools",
        description="CLI for Liora healthcare platform APIs. Outputs JSON.",
    )
    parser.add_argument(
        "--agent-id", default=os.environ.get("AGENT_ID", ""),
        help="Agent ID for portal activity reporting (default: $AGENT_ID)",
    )
    subs = parser.add_subparsers(dest="platform", required=True)

    # ── Weave ──

    weave = subs.add_parser("weave", help="Weave SMS, contacts, calls, voicemail")
    w = weave.add_subparsers(dest="command", required=True)

    p = w.add_parser("list-threads", help="List SMS threads")
    _add_page_size(p)
    p.set_defaults(func=weave_list_threads)

    p = w.add_parser("get-thread", help="Get a single SMS thread")
    p.add_argument("--id", required=True, help="Thread ID")
    _add_page_size(p)
    p.set_defaults(func=weave_get_thread)

    p = w.add_parser("send-message", help="Send an SMS (safety-guarded)")
    p.add_argument("--phone", required=True, help="Recipient phone (E.164 or 10-digit)")
    p.add_argument("--body", required=True, help="Message text")
    p.add_argument("--person-id", default=None, help="Weave person ID (optional)")
    p.set_defaults(func=weave_send_message)

    p = w.add_parser("search-contacts", help="Search Weave contacts")
    p.add_argument("--query", required=True, help="Search query")
    _add_page_size(p)
    p.set_defaults(func=weave_search_contacts)

    p = w.add_parser("lookup-phone", help="Look up a contact by phone number")
    p.add_argument("--phone", required=True, help="Phone number")
    p.set_defaults(func=weave_lookup_phone)

    p = w.add_parser("get-person", help="Get a contact by person ID")
    p.add_argument("--id", required=True, help="Person ID")
    p.set_defaults(func=weave_get_person)

    p = w.add_parser("list-call-records", help="List recent call records")
    _add_page_size(p)
    p.set_defaults(func=weave_list_call_records)

    p = w.add_parser("list-voicemails", help="List voicemail messages")
    _add_page_size(p)
    p.set_defaults(func=weave_list_voicemails)

    p = w.add_parser("dial", help="Initiate an outbound call (safety-guarded)")
    p.add_argument("--phone", required=True, help="Destination phone")
    p.set_defaults(func=weave_dial)

    # ── EMA ──

    ema = subs.add_parser("ema", help="ModMed EMA patients, appointments, scheduling")
    e = ema.add_subparsers(dest="command", required=True)

    p = e.add_parser("search-patients", help="Search patients by name")
    p.add_argument("--last-name", default=None)
    p.add_argument("--first-name", default=None)
    _add_page_size(p)
    p.set_defaults(func=ema_search_patients)

    p = e.add_parser("get-patient", help="Get patient by ID")
    p.add_argument("--id", required=True, help="Patient ID")
    p.set_defaults(func=ema_get_patient)

    p = e.add_parser("list-appointments", help="List appointments in a date range")
    p.add_argument("--start-date", default=None, help="YYYY-MM-DD")
    p.add_argument("--end-date", default=None, help="YYYY-MM-DD")
    _add_page_size(p, default=50)
    p.set_defaults(func=ema_list_appointments)

    p = e.add_parser("get-appointment", help="Get appointment by ID")
    p.add_argument("--id", required=True, help="Appointment ID")
    p.set_defaults(func=ema_get_appointment)

    p = e.add_parser("find-slots", help="Find available appointment slots")
    p.add_argument("--type-id", required=True, help="Appointment type ID")
    p.add_argument("--date", default=None, help="Specific date YYYY-MM-DD (default: first available)")
    p.add_argument("--duration", type=int, default=15, help="Duration in minutes")
    p.set_defaults(func=ema_find_slots)

    p = e.add_parser("cancel-appointment", help="Cancel an appointment")
    p.add_argument("--id", required=True, help="Appointment ID")
    p.add_argument("--reason", default="PATIENT_CANCELLED",
                   help="Cancel reason (default: PATIENT_CANCELLED)")
    p.add_argument("--notes", default="", help="Cancellation notes")
    p.set_defaults(func=ema_cancel_appointment)

    p = e.add_parser("reschedule", help="Reschedule an appointment")
    p.add_argument("--id", required=True, help="Appointment ID")
    p.add_argument("--start", required=True, help="New start time (ISO 8601 UTC)")
    p.add_argument("--duration", type=int, default=None, help="New duration in minutes")
    p.set_defaults(func=ema_reschedule)

    p = e.add_parser("list-appointment-types", help="List all appointment types")
    p.set_defaults(func=ema_list_appointment_types)

    p = e.add_parser("list-cancel-reasons", help="List available cancel reasons")
    p.set_defaults(func=ema_list_cancel_reasons)

    # ── ZocDoc ──

    zocdoc = subs.add_parser("zocdoc", help="ZocDoc inbox, bookings, messaging")
    z = zocdoc.add_subparsers(dest="command", required=True)

    p = z.add_parser("list-bookings", help="List inbox bookings")
    p.add_argument("--status", default=None,
                   help="Comma-separated statuses (e.g. UNCONFIRMED,PATIENT_RESCHEDULED)")
    p.add_argument("--patient-name", default=None, help="Filter by patient name")
    p.add_argument("--page", type=int, default=1, help="Page number")
    _add_page_size(p, default=20)
    p.set_defaults(func=zocdoc_list_bookings)

    p = z.add_parser("get-booking", help="Get booking details with PHI")
    p.add_argument("--id", required=True, help="Appointment ID")
    p.set_defaults(func=zocdoc_get_booking)

    p = z.add_parser("get-status-counts", help="Get appointment status aggregates")
    p.set_defaults(func=zocdoc_get_status_counts)

    p = z.add_parser("mark-as-read", help="Mark a booking as confirmed/read")
    p.add_argument("--id", required=True, help="Appointment ID")
    p.set_defaults(func=zocdoc_mark_as_read)

    p = z.add_parser("send-call-request",
                     help="Send 'call the office' request (uses browser for DataDome bypass)")
    p.add_argument("--id", required=True, help="Request/appointment ID")
    p.add_argument("--reasons", default=None, help="Comma-separated reasons")
    p.set_defaults(func=zocdoc_send_call_request)

    # ── Auth ──

    auth = subs.add_parser("auth", help="Session management and credential validation")
    a = auth.add_subparsers(dest="command", required=True)

    p = a.add_parser("check", help="Validate all platform sessions")
    p.set_defaults(func=auth_check)

    p = a.add_parser("refresh", help="Refresh a platform session via browser login")
    p.add_argument("target", choices=["weave", "ema", "zocdoc"],
                   help="Platform to refresh")
    p.set_defaults(func=auth_refresh)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    try:
        args.func(args)
    except LioraAPIError as e:
        _error(f"{type(e).__name__}: {e}")
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as e:
        _error(f"{type(e).__name__}: {e}")
