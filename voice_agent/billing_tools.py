"""P3 read-only Genie billing voice tools.

Ship only: get_patient_balance, get_weave_pay_link, get_visit_finance.
No live charge, card capture, invoice create, TTP POST, statement PDF, or brand+last4.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any, Callable

from .ops_tools import strip_pan_like

logger = logging.getLogger(__name__)

# Charge selector — no nested patient DOB/MRN/SSN
_CHARGE_SELECTOR = (
    "id,patient(id),patientResponsibleBalance,actualAmount,status,"
    "description,resolved,serviceDateLd"
)

_PAY_URL_PREFIX = "https://app.getweave.com/pay/"

# Keys stripped from any model-facing JSON (PCI / tender subtrees)
_PAYMENT_TREE_KEYS = frozenset({
    "payment",
    "payments",
    "paymentDetails",
    "payment_details",
})

# Extra PCI field names never returned even if they leak outside payment trees
_PCI_FIELD_KEYS = frozenset({
    "lastFour",
    "last4",
    "last_four",
    "brand",
    "cardholderName",
    "cardholder_name",
    "confirmationCode",
    "confirmation_code",
    "bankAccountNumber",
    "bankRoutingNumber",
    "bankAccount",
    "routingNumber",
    "accountNumber",
})

BILLING_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "get_patient_balance",
        "description": (
            "Read-only patient amount due from EMA charges. "
            "Requires patient_id from lookup_patient after ID verify. "
            "Returns amount_due and short open item labels. Never invent balances."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patient_id": {
                    "type": "integer",
                    "description": "EMA patient id from lookup_patient",
                },
            },
            "required": ["patient_id"],
        },
    },
    {
        "type": "function",
        "name": "get_weave_pay_link",
        "description": (
            "Look up an existing unpaid Weave online pay link (GET search only). "
            "Does not create invoices or send text-to-pay. "
            "Prefer weave_person_id; else phone for person resolve."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "weave_person_id": {
                    "type": "string",
                    "description": "Weave person UUID if known",
                },
                "phone": {
                    "type": "string",
                    "description": "Phone to resolve Weave person when id unknown",
                },
                "patient_id": {
                    "type": "integer",
                    "description": "EMA patient id for logging only",
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "get_visit_finance",
        "description": (
            "Read-only visit-day balance and paid copay from EMA "
            "appointments-finance-info. Use for today's visit / check-in, not full AR."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "appointment_id": {
                    "type": "integer",
                    "description": "EMA appointment id",
                },
            },
            "required": ["appointment_id"],
        },
    },
]


def _compact_json(data: Any) -> str:
    return json.dumps(data, default=str, separators=(",", ":"))


def _speak_result(
    *,
    status: str,
    message: str,
    speak: str | None = None,
    **extra: Any,
) -> str:
    out = {"status": status, "message": message, "speak": speak if speak is not None else message}
    out.update(extra)
    return _compact_json(out)


@lru_cache(maxsize=1)
def _get_ema_client():
    from liora_tools.auth.session_manager import get_ema_client

    return get_ema_client()


@lru_cache(maxsize=1)
def _get_weave_client():
    from liora_tools.auth.session_manager import get_weave_client

    return get_weave_client()


def clear_billing_caches() -> None:
    _get_ema_client.cache_clear()
    _get_weave_client.cache_clear()


def strip_payment_trees(obj: Any) -> Any:
    """Remove payment / payments / paymentDetails trees and known PCI keys."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if k in _PAYMENT_TREE_KEYS or k in _PCI_FIELD_KEYS:
                continue
            out[k] = strip_payment_trees(v)
        return out
    if isinstance(obj, list):
        return [strip_payment_trees(v) for v in obj]
    if isinstance(obj, str):
        return strip_pan_like(obj)
    return obj


def _amount_speak(amount: float) -> str:
    """Phone-friendly dollar amount; two decimals when needed."""
    try:
        a = float(amount)
    except (TypeError, ValueError):
        return "$0.00"
    if abs(a - round(a)) < 1e-9:
        return f"${int(round(a))}"
    return f"${a:,.2f}"


def _safe_float(val: Any) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _as_of_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _date_speak(service_date: str | None) -> str:
    if not service_date:
        return ""
    s = str(service_date).strip()
    # YYYY-MM-DD or longer ISO
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if not m:
        return s
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    months = (
        "", "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    )
    if 1 <= mo <= 12:
        return f"{months[mo]} {d}, {y}"
    return s


def _fetch_all_charges(client: Any, patient_id: str | int) -> list[dict]:
    """Page charges until empty page (pageSize=100)."""
    rows: list[dict] = []
    page = 1
    while True:
        batch = client.list_charges(
            where=f"patient=={patient_id}",
            page_size=100,
            page_number=page,
            selector=_CHARGE_SELECTOR,
        )
        if not isinstance(batch, list):
            # Some clients return {"data": [...]} — accept either
            if isinstance(batch, dict):
                batch = batch.get("data") or batch.get("results") or batch.get("charges") or []
            else:
                batch = []
        if not batch:
            break
        rows.extend(r for r in batch if isinstance(r, dict))
        if len(batch) < 100:
            break
        page += 1
        if page > 50:  # hard safety cap
            break
    return rows


def get_patient_balance(arguments: dict) -> str:
    """EMA charges aggregate — primary amount-due tool."""
    from liora_tools.exceptions import AuthenticationError

    patient_id = arguments.get("patient_id")
    if patient_id is None or patient_id == "":
        msg = (
            "I need to confirm who I'm speaking with first — date of birth is perfect."
        )
        return _speak_result(status="patient_id_required", message=msg)

    try:
        client = _get_ema_client()
        charges = _fetch_all_charges(client, patient_id)
    except AuthenticationError as e:
        logger.warning("get_patient_balance session expired: %s", e)
        msg = (
            "I can't pull your balance right this second. "
            "I can connect you with billing, or you can try the patient portal."
        )
        return _speak_result(
            status="session_expired",
            message=msg,
            amount_due=None,
            currency="USD",
            source="ema_charges",
            as_of=_as_of_iso(),
            patient_id=patient_id,
            detail=str(e),
        )
    except Exception as e:
        logger.exception("get_patient_balance failed")
        msg = (
            "I can't pull your balance right this second. "
            "I can connect you with billing, or you can try the patient portal."
        )
        return _speak_result(
            status="lookup_failed",
            message=msg,
            amount_due=None,
            currency="USD",
            source="ema_charges",
            as_of=_as_of_iso(),
            patient_id=patient_id,
            detail=str(e),
        )

    open_items: list[dict[str, Any]] = []
    amount_due = 0.0
    for row in charges:
        bal = _safe_float(row.get("patientResponsibleBalance"))
        status = str(row.get("status") or "").upper()
        if bal is None or bal <= 0 or status != "CHARGED":
            continue
        amount_due += bal
        desc = row.get("description")
        if isinstance(desc, str):
            desc = strip_pan_like(desc.strip()) or None
        else:
            desc = None
        item = {
            "description": desc,
            "amount": round(bal, 2),
            "service_date": row.get("serviceDateLd") or None,
        }
        open_items.append(item)

    # Cap open_items at 5; prefer larger balances first
    open_items.sort(key=lambda x: float(x.get("amount") or 0), reverse=True)
    open_items = open_items[:5]
    amount_due = round(amount_due, 2)
    # Count all open charged lines (not just top 5 items)
    open_charge_count = sum(
        1
        for row in charges
        if (_safe_float(row.get("patientResponsibleBalance")) or 0) > 0
        and str(row.get("status") or "").upper() == "CHARGED"
    )

    as_of = _as_of_iso()
    if amount_due <= 0:
        msg = "I don't see an open balance on your account right now."
        return _speak_result(
            status="zero_balance",
            message=msg,
            amount_due=0.0,
            currency="USD",
            open_charge_count=0,
            open_items=[],
            as_of=as_of,
            source="ema_charges",
            patient_id=patient_id,
        )

    amt = _amount_speak(amount_due)
    if open_charge_count == 1 and open_items:
        it = open_items[0]
        desc = (it.get("description") or "charge").lower()
        # Soften all-caps CPT labels
        if desc.isupper():
            desc = desc.title()
        date_s = _date_speak(it.get("service_date"))
        if date_s:
            msg = f"I see {amt} open — looks like a {desc} from {date_s}."
        else:
            msg = f"I see a balance of {amt} on your account."
    elif open_charge_count > 1:
        msg = (
            f"I see {amt} total across {open_charge_count} open charges. "
            "I can give a quick breakdown or help with how to pay."
        )
    else:
        msg = f"I see a balance of {amt} on your account."

    return _speak_result(
        status="ok",
        message=msg,
        amount_due=amount_due,
        currency="USD",
        open_charge_count=open_charge_count,
        open_items=open_items,
        as_of=as_of,
        source="ema_charges",
        patient_id=patient_id,
    )


def _extract_person_id(lookup: Any) -> str | None:
    if not isinstance(lookup, dict):
        return None
    # Common Weave shapes
    for key in ("id", "personId", "person_id"):
        if lookup.get(key):
            return str(lookup[key])
    person = lookup.get("person") or lookup.get("primaryContact") or lookup.get("contact")
    if isinstance(person, dict):
        for key in ("id", "personId"):
            if person.get(key):
                return str(person[key])
    # Nested data
    data = lookup.get("data")
    if isinstance(data, dict):
        return _extract_person_id(data)
    return None


def _invoice_rows(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("invoices", "data", "results", "items"):
        val = payload.get(key)
        if isinstance(val, list):
            return [r for r in val if isinstance(r, dict)]
    return []


def _is_active_unpaid(inv: dict) -> bool:
    status = str(inv.get("status") or "").upper()
    if status == "PAID":
        return False
    link = inv.get("uniqueLink")
    if not link:
        return False
    # Prefer isActive true; still accept if status unpaid-like and link present
    is_active = inv.get("isActive")
    if is_active is False and status not in {"UNPAID", "OPEN", "PENDING", "DUE"}:
        return False
    if is_active is False and status in {"UNPAID", "OPEN", "PENDING", "DUE"}:
        # edge: some unpaid still flagged inactive — require unpaid-like status
        return True
    if is_active is True:
        return status != "PAID"
    # isActive missing — treat non-PAID with uniqueLink as candidate
    return status != "PAID"


def get_weave_pay_link(arguments: dict) -> str:
    """GET-only Weave invoice search for an existing unpaid pay URL."""
    from liora_tools.exceptions import AuthenticationError

    weave_person_id = arguments.get("weave_person_id") or arguments.get("person_id")
    phone = arguments.get("phone")
    patient_id = arguments.get("patient_id")  # logging only

    person_id = str(weave_person_id).strip() if weave_person_id else None

    try:
        client = _get_weave_client()
        if not person_id and phone:
            try:
                lookup = client.lookup_by_phone(str(phone))
                person_id = _extract_person_id(lookup)
            except Exception as e:
                logger.warning("weave lookup_by_phone failed: %s", e)

        if not person_id:
            msg = (
                "I don't see an active online invoice I can send from here. "
                "Billing can text a pay link or take care of it — want me to connect you?"
            )
            return _speak_result(
                status="person_unresolved",
                message=msg,
                found=False,
                patient_id=patient_id,
            )

        raw = client.search_invoices(person_id=person_id, limit=25, skip=0)
        # Strip PCI before any further processing exposed to model
        safe = strip_payment_trees(raw)
        invoices = _invoice_rows(safe)
        # Also strip each invoice again in case rows came from list path
        invoices = [strip_payment_trees(i) for i in invoices]

        unpaid = [i for i in invoices if _is_active_unpaid(i)]
        if not unpaid:
            msg = (
                "I don't see an active online invoice I can send from here. "
                "Billing can text a pay link or take care of it — want me to connect you?"
            )
            return _speak_result(
                status="none",
                message=msg,
                found=False,
                has_pay_link=False,
                weave_person_id=person_id,
                patient_id=patient_id,
            )

        # Prefer active unpaid with highest billedAmount
        def _billed(inv: dict) -> float:
            cents = _safe_float(inv.get("billedAmount")) or 0.0
            return cents

        unpaid.sort(key=_billed, reverse=True)
        chosen = unpaid[0]
        unique = str(chosen.get("uniqueLink") or "").strip()
        if not unique:
            msg = (
                "I don't see an active online invoice I can send from here. "
                "Billing can text a pay link or take care of it — want me to connect you?"
            )
            return _speak_result(
                status="none",
                message=msg,
                found=False,
                has_pay_link=False,
                weave_person_id=person_id,
                patient_id=patient_id,
            )

        pay_url = f"{_PAY_URL_PREFIX}{unique}"
        cents = _safe_float(chosen.get("billedAmount"))
        amount_due = round(cents / 100.0, 2) if cents is not None else None
        inv_status = str(chosen.get("status") or "UNPAID").upper()
        amt_s = _amount_speak(amount_due) if amount_due is not None else "the invoice amount"
        msg = (
            f"You can pay online — I can give you the secure pay link. "
            f"It's also best if we text it so you can tap it. "
            f"The amount on that invoice is {amt_s}."
        )
        result = {
            "status": "found",
            "message": msg,
            "speak": msg,
            "found": True,
            "has_pay_link": True,
            "pay_url": pay_url,
            "currency": "USD",
            "status_invoice": inv_status,
            "weave_person_id": person_id,
            "patient_id": patient_id,
        }
        if amount_due is not None:
            result["amount_due"] = amount_due
        # Final PCI pass on serialized structure
        return _compact_json(strip_payment_trees(result))

    except AuthenticationError as e:
        logger.warning("get_weave_pay_link auth failed: %s", e)
        msg = (
            "I can't check for an online pay link right now. "
            "I can connect you with billing instead."
        )
        return _speak_result(
            status="lookup_failed",
            message=msg,
            found=False,
            patient_id=patient_id,
            detail=str(e),
        )
    except Exception as e:
        logger.exception("get_weave_pay_link failed")
        msg = (
            "I can't check for an online pay link right now. "
            "I can connect you with billing instead."
        )
        return _speak_result(
            status="lookup_failed",
            message=msg,
            found=False,
            patient_id=patient_id,
            detail=str(e),
        )


def _gmt_window_around(start_iso: str | None, facility_id: str = "2040") -> str:
    """Build scheduler-style where with facility + date window around appointment."""
    # Default: ±3 days around now if no start
    center = datetime.now(timezone.utc)
    if start_iso:
        s = str(start_iso).replace("Z", "+00:00")
        # EMA sometimes uses +0000
        s = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", s)
        try:
            center = datetime.fromisoformat(s)
            if center.tzinfo is None:
                center = center.replace(tzinfo=timezone.utc)
        except ValueError:
            # Try date-only
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", str(start_iso))
            if m:
                center = datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    tzinfo=timezone.utc,
                )

    start = center - timedelta(days=3)
    end = center + timedelta(days=3)

    def _fmt(dt: datetime) -> str:
        # e.g. Mon Mar 09 2026 00:00:00 GMT-0400 — use UTC GMT+0000 for stability
        # Python: %z is +0000
        local = dt.astimezone(timezone.utc)
        # Weekday Mon, month Mar
        return local.strftime("%a %b %d %Y %H:%M:%S GMT%z")

    start_s = _fmt(start.replace(hour=0, minute=0, second=0, microsecond=0))
    end_s = _fmt(end.replace(hour=23, minute=59, second=59, microsecond=0))
    return (
        f'(facility=in=("{facility_id}") or facility=null="true")'
        f' and scheduledStartDate>="{start_s}"'
        f' and scheduledEndDate<="{end_s}"'
    )


def get_visit_finance(arguments: dict) -> str:
    """Visit-day balance / paid copay from appointments-finance-info."""
    from liora_tools.exceptions import AuthenticationError
    from liora_tools.config import EmaConfig

    appointment_id = arguments.get("appointment_id")
    if appointment_id is None or appointment_id == "":
        msg = "I need which visit you mean before I can check that balance."
        return _speak_result(status="appointment_id_required", message=msg)

    try:
        appt_id_int = int(appointment_id)
    except (TypeError, ValueError):
        msg = "I need which visit you mean before I can check that balance."
        return _speak_result(status="appointment_id_required", message=msg)

    facility_id = str(EmaConfig().facility_id or "2040")

    try:
        client = _get_ema_client()
        start_iso = None
        try:
            appt = client.get_appointment(
                str(appt_id_int),
                selector="id,scheduledStartDate,scheduledEndDate,facility(id)",
            )
            if isinstance(appt, dict):
                start_iso = appt.get("scheduledStartDate")
                fac = appt.get("facility") or {}
                if isinstance(fac, dict) and fac.get("id") is not None:
                    facility_id = str(fac["id"])
        except Exception as e:
            logger.warning("get_appointment for finance window failed: %s", e)

        where = _gmt_window_around(start_iso, facility_id=facility_id)
        rows = client.get_appointments_finance_info(where=where)
        if isinstance(rows, dict):
            rows = rows.get("data") or rows.get("results") or []
        if not isinstance(rows, list):
            rows = []

        match = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            rid = row.get("appointmentId")
            if rid is None:
                rid = row.get("appointment_id") or row.get("id")
            try:
                if int(rid) == appt_id_int:
                    match = row
                    break
            except (TypeError, ValueError):
                if str(rid) == str(appt_id_int):
                    match = row
                    break

        if match is None:
            msg = "I'm not seeing finance details for that visit right now."
            return _speak_result(
                status="not_found",
                message=msg,
                appointment_id=appt_id_int,
                source="ema_finance_info",
            )

        balance = _safe_float(match.get("balance"))
        paid_copay = _safe_float(match.get("paidCopay"))
        if paid_copay is None:
            paid_copay = _safe_float(match.get("paid_copay"))

        # Never invent — only speak numbers we actually got
        if balance is None and paid_copay is None:
            msg = "I'm not seeing finance details for that visit right now."
            return _speak_result(
                status="not_found",
                message=msg,
                appointment_id=appt_id_int,
                source="ema_finance_info",
            )

        if balance is not None and abs(balance) > 1e-9:
            msg = f"For that visit I'm seeing a balance of {_amount_speak(balance)}."
        elif paid_copay is not None and paid_copay > 0:
            msg = (
                f"It looks like a copay of {_amount_speak(paid_copay)} "
                "was already paid for that visit."
            )
        else:
            msg = "I'm not seeing a balance on that visit."

        extra: dict[str, Any] = {
            "appointment_id": appt_id_int,
            "source": "ema_finance_info",
        }
        if balance is not None:
            extra["balance"] = round(balance, 2)
        if paid_copay is not None:
            extra["paid_copay"] = round(paid_copay, 2)

        return _speak_result(status="ok", message=msg, **extra)

    except AuthenticationError as e:
        logger.warning("get_visit_finance session expired: %s", e)
        msg = (
            "I can't pull visit balance right this second. "
            "I can connect you with billing if you'd like."
        )
        return _speak_result(
            status="lookup_failed",
            message=msg,
            appointment_id=appt_id_int,
            source="ema_finance_info",
            detail=str(e),
        )
    except Exception as e:
        logger.exception("get_visit_finance failed")
        msg = (
            "I can't pull visit balance right this second. "
            "I can connect you with billing if you'd like."
        )
        return _speak_result(
            status="lookup_failed",
            message=msg,
            appointment_id=appt_id_int,
            source="ema_finance_info",
            detail=str(e),
        )


_HANDLERS: dict[str, Callable[[dict], str]] = {
    "get_patient_balance": get_patient_balance,
    "get_weave_pay_link": get_weave_pay_link,
    "get_visit_finance": get_visit_finance,
}


def handle_billing_tool(name: str, arguments: dict) -> str:
    """Execute a billing tool; return JSON string for Grok."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return _speak_result(
            status="unknown_tool",
            message="I can't do that from here.",
            error="unknown_tool",
            name=name,
        )
    try:
        return handler(arguments or {})
    except Exception as e:
        logger.exception("billing tool %s failed", name)
        return _speak_result(
            status="tool_failed",
            message="Something went wrong on my end — let me have someone call you back.",
            error="billing_tool_failed",
            tool=name,
            detail=str(e),
        )


BILLING_TOOL_NAMES = frozenset(_HANDLERS.keys())
