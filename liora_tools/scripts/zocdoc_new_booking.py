"""Zocdoc new-patient booking processor (production job).

Scans recent Zocdoc bookings for NEW patients, runs the fee-avoidance
call-request → EMA portal → Weave SMS path, and reports every state to
Genies Bottle under a stable correlation_id.

Usage:
    python -m liora_tools.scripts.zocdoc_new_booking --dry-run
    python -m liora_tools run zocdoc-new-booking --dry-run
    python -m liora_tools run zocdoc-new-booking --lookback-minutes=90

Safety:
    - File lock prevents overlapping runs
    - correlation_id upserts GB executions (no double create)
    - Durable local StepLedger so resume works when GB omits steps
    - Step-level resume: re-runs skip call-request / SMS / portal if already done
    - Fee-gate checkpoint after call-request before portal/SMS
    - Template-first SMS only; PHI minimized in logs and GB payloads
"""

from __future__ import annotations

import argparse
import atexit
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv as _load_dotenv
except ImportError:  # pragma: no cover
    def _load_dotenv(*_a, **_k):
        return False

# Prefer package .env then cwd
_PKG_ROOT = Path(__file__).resolve().parents[2]
_load_dotenv(_PKG_ROOT / ".env")
_load_dotenv()

TASK_SLUG = "zocdoc-new-booking"
SMS_TEMPLATE_ID = "00914ffc-ae68-49c8-a76d-a0d78a5d5d21"
SMS_TEMPLATE_NAME = "Genie - New Zocdoc Patient"
# Only merge fields the job may substitute. Unknown {{VARS}} are rejected.
SMS_ALLOWED_VARS = frozenset({"FIRST_NAME"})
_TEMPLATE_VAR_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
# Full production template (runbook SoT). Prefer this over Weave templator
# when API returns 401 / empty — never use a shorter alternate body.
SMS_TEMPLATE_BODY = (
    "Hello {{FIRST_NAME}} ,\n"
    "\n"
    "Thanks for scheduling with us at Liora.\n"
    "\n"
    "In order to confirm your appointment, please log into the portal "
    "(link just sent) and complete the registration, including adding a "
    "credit card on file (securely encrypted).\n"
    "\n"
    "Because appointments scheduled through Zocdoc reserve dedicated provider "
    "time and incur a booking cost of $100 to the practice, we require all new "
    "patients to complete registration and maintain a card on file prior to "
    "confirming the visit. If the registration is not completed, we may need "
    "to release the appointment so it can be offered to another patient in "
    "need of care.\n"
    "\n"
    "Please let us know if you need the portal link resent or if we can assist "
    "you in any way.\n"
    "\n"
    "We look forward to hearing from you soon!"
)
# Distinctive phrase used when searching Weave for prior Genie SMS
SMS_FINGERPRINT = "booking cost of $100"

DEFAULT_LOCK_PATH = os.path.expanduser("~/.liora/locks/zocdoc-new-booking.lock")
DEFAULT_STATE_PATH = os.path.expanduser("~/.liora/state/zocdoc-new-booking.json")
DEFAULT_LOOKBACK_MINUTES = 90
LEDGER_RETENTION_DAYS = 14

# Prefer terminal success over failed/in-progress when merging step lists
_STEP_STATUS_RANK = {
    "done": 40,
    "completed": 40,
    "ok": 40,
    "skipped": 35,
    "failed": 10,
    "error": 10,
    "in_progress": 5,
    "running": 5,
    "pending": 1,
}

FLOW_DEFINITION = {
    "filters": [
        {"name": "recent_bookings", "description": "bookingTimeUtc within lookback window"},
        {"name": "new_patients", "description": "patientType == NEW"},
        {"name": "not_cancelled", "description": "status != PATIENT_CANCELLED"},
    ],
    "gates": [
        {
            "name": "already_processed",
            "service": "genies-bottle",
            "description": "Skip if completed execution exists for correlation_id",
        },
        {
            "name": "already_contacted",
            "service": "weave",
            "description": "Skip SMS if Genie $100 template fingerprint already present",
        },
    ],
    "steps": [
        {"name": "get_booking_details", "service": "zocdoc"},
        {"name": "send_call_request", "service": "zocdoc", "note": "$100 fee avoidance"},
        {"name": "activate_portal", "service": "ema"},
        {
            "name": "send_welcome_sms",
            "service": "weave",
            "template_id": SMS_TEMPLATE_ID,
            "template_name": SMS_TEMPLATE_NAME,
            "allowed_vars": sorted(SMS_ALLOWED_VARS),
        },
        {"name": "report_completed", "service": "genies-bottle"},
    ],
}

# ── PHI-safe helpers ─────────────────────────────────────────────────────────


def _mask_phone(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 4:
        return "(none)" if not digits else "****"
    return f"***{digits[-4:]}"


def _mask_email(email: str | None) -> str:
    if not email or "@" not in email:
        return "(none)"
    local, _, domain = email.partition("@")
    if not local:
        return f"*@{domain}"
    return f"{local[0]}***@{domain}"


def _mask_name(name: str | None) -> str:
    name = (name or "").strip()
    if not name:
        return "(unknown)"
    parts = name.split()
    return " ".join((p[0] + "***") if p else "" for p in parts)


def _patient_gb_payload(mrn: str, name: str, phone: str | None = None) -> dict:
    """Minimal patient dict for GB — masked name, no full phone/email."""
    out: dict[str, Any] = {"mrn": str(mrn), "name": _mask_name(name)}
    if phone:
        out["phone_last4"] = re.sub(r"\D", "", phone)[-4:]
    return out


def _redact_error(err: BaseException | str) -> str:
    text = str(err)
    # Secrets before PII so token-like digit runs inside JWTs are gone first
    text = re.sub(
        r"Bearer\s+\S+",
        "Bearer [redacted]",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9._-]+",
        "[token]",
        text,
    )
    text = re.sub(
        r"(api[_-]?key|secret|password|token)\s*[:=]\s*\S+",
        r"\1=[redacted]",
        text,
        flags=re.IGNORECASE,
    )
    # Strip obvious emails / long digit runs that may be phone/MRN
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[email]", text)
    text = re.sub(r"\+?\d[\d\s().-]{8,}\d", "[phone]", text)
    return text[:500]


# ── Correlation + steps ──────────────────────────────────────────────────────


def build_correlation_id(appointment_id: str, mrn: str | None = None,
                         appt_date: str | None = None) -> str:
    """Stable id for GB upsert.

    Prefer appointmentId (unique per Zocdoc booking). Fall back to
    zocdoc-{mrn}-{date} when appointment id is missing.
    """
    appt = (appointment_id or "").strip()
    if appt:
        # Keep human-readable; sanitize path-ish chars
        safe = re.sub(r"[^A-Za-z0-9_.:-]+", "-", appt)[:180]
        return f"zocdoc-{safe}"
    mrn_s = re.sub(r"[^A-Za-z0-9-]+", "", str(mrn or "unknown"))[:64]
    date_s = (appt_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"))[:10]
    return f"zocdoc-{mrn_s}-{date_s}"


def step_done(steps: list | None, *names: str) -> bool:
    """True if any named step is already marked done."""
    if not steps:
        return False
    want = {n.lower() for n in names}
    for s in steps:
        if not isinstance(s, dict):
            continue
        status = str(s.get("status") or "").lower()
        if status not in ("done", "completed", "ok", "skipped"):
            continue
        label = " ".join(
            str(s.get(k) or "") for k in ("name", "action", "step")
        ).lower()
        if any(n in label for n in want):
            return True
        # numeric step map from runbook
        num = s.get("step")
        if num == 2 and any("call" in n for n in want):
            return True
        if num == 3 and any("portal" in n for n in want):
            return True
        if num == 4 and any("sms" in n or "weave" in n for n in want):
            return True
    return False


def make_step(num: int, action: str, status: str, detail: str | None = None) -> dict:
    out = {"step": num, "name": action, "action": action, "status": status}
    if detail:
        out["detail"] = detail
    return out


class JobStepError(Exception):
    """Actionable per-step failure with in-flight steps attached (no PHI dump)."""

    def __init__(
        self,
        message: str,
        *,
        step: str,
        correlation_id: str,
        next_action: str,
        steps: list | None = None,
    ):
        self.message = _redact_error(message)
        self.step = step
        self.correlation_id = correlation_id
        self.next_action = next_action
        self.steps = list(steps or [])
        super().__init__(str(self))

    def __str__(self) -> str:
        return (
            f"step={self.step} corr={self.correlation_id} failed: {self.message} "
            f"| next: {self.next_action}"
        )


def _step_status_rank(status: Any) -> int:
    return _STEP_STATUS_RANK.get(str(status or "").lower(), 0)


def merge_step_lists(*lists: list | None) -> list[dict]:
    """Union step dicts by step number; prefer done/skipped over failed/in-progress."""
    by_num: dict[int, dict] = {}
    extras: list[dict] = []
    for lst in lists:
        if not lst:
            continue
        for s in lst:
            if not isinstance(s, dict):
                continue
            num = s.get("step")
            if not isinstance(num, int):
                try:
                    num = int(num)
                except (TypeError, ValueError):
                    extras.append(dict(s))
                    continue
            cur = by_num.get(num)
            if cur is None or _step_status_rank(s.get("status")) > _step_status_rank(
                cur.get("status")
            ):
                by_num[num] = dict(s)
    out = [by_num[k] for k in sorted(by_num)]
    out.extend(extras)
    return out


class StepLedger:
    """Durable local step ledger keyed by correlation_id.

    GB webhook GET /api/webhooks/executions currently omits `steps`, so resume
    cannot rely on prior GB payloads alone. This file is the fee/SMS source of
    truth on the job side.
    """

    def __init__(self, path: str | None = None):
        env_path = os.environ.get("ZOCDOC_NEW_BOOKING_STATE_PATH")
        self.path = os.path.expanduser(
            path or env_path or DEFAULT_STATE_PATH
        )
        self._cache: dict | None = None

    def load(self) -> dict:
        if self._cache is not None:
            return self._cache
        p = Path(self.path)
        if not p.is_file():
            self._cache = {"version": 1, "entries": {}}
            return self._cache
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._cache = {"version": 1, "entries": {}}
            return self._cache
        if not isinstance(raw, dict):
            raw = {}
        entries = raw.get("entries")
        if not isinstance(entries, dict):
            entries = {}
        self._cache = {"version": int(raw.get("version") or 1), "entries": entries}
        return self._cache

    def save(self) -> None:
        data = self.load()
        self._prune(data)
        p = Path(self.path)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(data, indent=2, sort_keys=True)
        # Atomic write: tmp in same dir then replace
        fd, tmp_name = tempfile.mkstemp(
            prefix=".zocdoc-ledger-", suffix=".tmp", dir=str(p.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _prune(self, data: dict) -> None:
        entries = data.get("entries")
        if not isinstance(entries, dict):
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=LEDGER_RETENTION_DAYS)
        drop: list[str] = []
        for key, ent in entries.items():
            if not isinstance(ent, dict):
                drop.append(key)
                continue
            ts = ent.get("updated_at") or ent.get("created_at")
            if not ts:
                continue
            try:
                dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            if dt < cutoff:
                drop.append(key)
        for key in drop:
            entries.pop(key, None)

    def get(self, correlation_id: str) -> dict | None:
        entries = self.load().get("entries") or {}
        ent = entries.get(correlation_id)
        return dict(ent) if isinstance(ent, dict) else None

    def record(
        self,
        correlation_id: str,
        *,
        steps: list | None = None,
        status: str | None = None,
        appointment_id: str | None = None,
    ) -> dict:
        """Merge steps for correlation_id and persist immediately."""
        data = self.load()
        entries = data.setdefault("entries", {})
        prev = entries.get(correlation_id) if isinstance(entries.get(correlation_id), dict) else {}
        merged_steps = merge_step_lists(prev.get("steps") if prev else None, steps)
        now = datetime.now(timezone.utc).isoformat()
        entry: dict[str, Any] = {
            "steps": merged_steps,
            "updated_at": now,
        }
        if status is not None:
            entry["status"] = status
        elif prev and prev.get("status"):
            entry["status"] = prev["status"]
        # Minimal non-PHI metadata only
        appt = appointment_id or (prev.get("appointment_id") if prev else None)
        if appt:
            entry["appointment_id"] = str(appt)
        entries[correlation_id] = entry
        self.save()
        return dict(entry)

    def merge_steps(self, correlation_id: str, steps: list | None) -> list[dict]:
        """Return union of ledger steps and provided list (does not persist)."""
        ent = self.get(correlation_id) or {}
        return merge_step_lists(ent.get("steps"), steps)


def booking_call_already_requested(appt_details: dict | None) -> bool:
    """True if Zocdoc PHI shows patient.requestedToCallTimestamp (call already sent)."""
    if not isinstance(appt_details, dict):
        return False

    def _has_ts(obj: Any) -> bool:
        if not isinstance(obj, dict):
            return False
        ts = obj.get("requestedToCallTimestamp")
        if ts is None or ts is False:
            return False
        if isinstance(ts, str) and not ts.strip():
            return False
        return True

    if _has_ts(appt_details):
        return True
    patient = appt_details.get("patient")
    if _has_ts(patient):
        return True
    # Walk one level of nested dicts under patient (defensive)
    if isinstance(patient, dict):
        for v in patient.values():
            if isinstance(v, dict) and _has_ts(v):
                return True
    return False


def call_request_error_is_already_done(err: BaseException | str) -> bool:
    """True only when API error unambiguously means call was already requested."""
    text = str(err).lower()
    needles = (
        "already requested",
        "already been requested",
        "call already requested",
        "request already exists",
        "patient call already",
        "already requested to call",
    )
    return any(n in text for n in needles)


# ── Bookings filter ──────────────────────────────────────────────────────────


def extract_candidates(bookings_data: dict, lookback_minutes: int = 60) -> list[dict]:
    """Filter list_bookings payload to NEW patients in the lookback window."""
    appointments = (
        bookings_data.get("data", {})
        .get("appointments", {})
        .get("appointments", [])
    )
    if not isinstance(appointments, list):
        appointments = []

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
    candidates: list[dict] = []

    for appt in appointments:
        if not isinstance(appt, dict):
            continue
        booking_time_str = appt.get("bookingTimeUtc")
        if not booking_time_str:
            continue
        try:
            booking_time = datetime.fromisoformat(
                str(booking_time_str).replace("Z", "+00:00")
            )
            if booking_time.tzinfo is None:
                booking_time = booking_time.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if booking_time < cutoff:
            continue
        if appt.get("patientType") != "NEW":
            continue
        if appt.get("appointmentStatus") == "PATIENT_CANCELLED":
            continue
        candidates.append(appt)

    # Most recent booking first (fee window is 24h from booking)
    def _key(a: dict) -> str:
        return str(a.get("bookingTimeUtc") or "")

    candidates.sort(key=_key, reverse=True)
    return candidates


def validate_sms_template(template: str) -> str:
    """Ensure body is an approved Genie new-patient template.

    Rejects free-form / alternate copy: must carry the $100 fingerprint,
    must declare {{FIRST_NAME}}, and may only use SMS_ALLOWED_VARS.
    Returns the stripped template or raises ValueError (no PHI in message).
    """
    body = (template or "").strip()
    if not body:
        raise ValueError("sms template empty")
    if SMS_FINGERPRINT not in body:
        raise ValueError("sms template missing required fingerprint")
    vars_found = _TEMPLATE_VAR_RE.findall(body)
    if "FIRST_NAME" not in vars_found:
        raise ValueError("sms template missing {{FIRST_NAME}}")
    unknown = sorted({v for v in vars_found if v not in SMS_ALLOWED_VARS})
    if unknown:
        raise ValueError(f"sms template has disallowed vars: {', '.join(unknown)}")
    return body


def build_sms_body(template: str, *, first_name: str) -> str:
    """Render approved template only. Refuses free-form or unsubstituted bodies."""
    body = validate_sms_template(template)
    name = (first_name or "").strip() or "there"
    # Only substitute allowed vars; never accept caller-built free-form text.
    rendered = body.replace("{{FIRST_NAME}}", name)
    # Also clear spaced forms if Weave returns {{ FIRST_NAME }}
    rendered = re.sub(r"\{\{\s*FIRST_NAME\s*\}\}", name, rendered)
    if "{{" in rendered or "}}" in rendered:
        raise RuntimeError("refusing to send unsubstituted SMS template vars")
    if SMS_FINGERPRINT not in rendered:
        raise RuntimeError("refusing to send non-template SMS (fingerprint lost)")
    if not rendered.strip():
        raise RuntimeError("refusing to send empty SMS body")
    return rendered


def render_sms(template: str, first_name: str) -> str:
    """Back-compat alias for build_sms_body."""
    return build_sms_body(template, first_name=first_name)


# ── Lock ─────────────────────────────────────────────────────────────────────


class JobLock:
    """Exclusive file lock so overlapping cron ticks do not double-act."""

    def __init__(self, path: str = DEFAULT_LOCK_PATH):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fh.close()
            return False
        except ImportError:
            # Non-POSIX: best-effort pid stamp
            fh.seek(0)
            existing = fh.read().strip()
            if existing and existing.isdigit():
                pid = int(existing)
                try:
                    os.kill(pid, 0)
                    fh.close()
                    return False
                except OSError:
                    pass
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()
        self._fh = fh
        atexit.register(self.release)
        return True

    def release(self) -> None:
        if not self._fh:
            return
        try:
            import fcntl
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            self._fh.close()
        except Exception:
            pass
        self._fh = None


# ── Clients ──────────────────────────────────────────────────────────────────


def _fetch_sms_template(weave) -> str:
    """Load approved template only.

    Weave templator body is accepted solely when it passes validate_sms_template
    (fingerprint + allowed vars). Otherwise use hardcoded runbook SoT body.
    Never returns free-form / alternate shorter copy.
    """
    fallback = validate_sms_template(SMS_TEMPLATE_BODY)
    try:
        from liora_tools.config import WeaveConfig
        cfg = WeaveConfig()
        r = weave._s.get(
            f"{cfg.api_base}/messaging/templator/v2/templates",
            params={"orgId": cfg.tenant_id},
            timeout=30,
        )
        if r.ok:
            templates = r.json()
            if isinstance(templates, dict):
                templates = templates.get("templates", [])
            for t in templates or []:
                tid = t.get("templateId") or t.get("id")
                tname = (t.get("name") or t.get("templateName") or "").strip()
                if tid != SMS_TEMPLATE_ID and tname != SMS_TEMPLATE_NAME:
                    continue
                body = t.get("templateString") or t.get("body")
                try:
                    return validate_sms_template(body or "")
                except ValueError:
                    # Remote copy drifted — keep SoT fallback
                    return fallback
    except Exception:
        pass
    return fallback


def init_clients(*, require_gb: bool = True):
    """Initialize clients via session_manager (Kernel bridge first)."""
    from liora_tools.auth.session_manager import get_client
    from liora_tools.genies_bottle.client import GenieBottleClient

    zoc = get_client("zocdoc")
    weave = get_client("weave")
    ema = get_client("ema")
    gb = None
    try:
        gb = GenieBottleClient.from_api_key()
    except ValueError as e:
        if require_gb:
            raise
        print(f"WARN: Genies Bottle unavailable ({e}); gates that need GB are skipped")
    sms_template = _fetch_sms_template(weave)
    return zoc, weave, ema, gb, sms_template


class _NullGB:
    """Dry-run stand-in when GENIE_BOTTLE_API_KEY is unset."""

    def query_executions(self, **_kwargs):
        return []

    def report_process(self, *a, **k):
        return {"id": "dry-run", "status": k.get("status") or (a[1] if len(a) > 1 else None)}

    def log_activity(self, *a, **k):
        return {"ok": True}

    def request_feedback(self, *a, **k):
        return {"ok": True}

    def _post(self, *a, **k):
        return {"ok": True}


# ── Gates ────────────────────────────────────────────────────────────────────


def gb_prior_execution(gb, correlation_id: str) -> dict | None:
    """Return most relevant prior execution for this correlation_id, if any."""
    try:
        rows = gb.query_executions(
            task_slug=TASK_SLUG,
            correlation_id=correlation_id,
            limit=10,
        )
    except Exception as e:
        raise RuntimeError(f"GB query_executions failed: {_redact_error(e)}") from e
    if not isinstance(rows, list):
        rows = rows.get("executions") or rows.get("data") or [] if isinstance(rows, dict) else []
    if not rows:
        return None
    # Prefer completed, then running, then failed
    order = {"completed": 0, "running": 1, "needs_review": 2, "failed": 3}
    rows = sorted(rows, key=lambda r: order.get(str(r.get("status")), 9))
    return rows[0]


def weave_already_sent_genie_sms(weave, first_name: str, last_name: str,
                                 phone: str | None) -> bool:
    """True if Weave already has the Genie new-patient SMS for this person."""
    queries = []
    if phone:
        digits = re.sub(r"\D", "", phone)
        if len(digits) >= 10:
            queries.append(digits[-10:])
    name = f"{first_name} {last_name}".strip()
    if name:
        queries.append(name)
    queries.append(SMS_FINGERPRINT)

    for q in queries:
        try:
            results = weave.search_messages(q)
        except Exception:
            continue
        n = int(results.get("numResults") or 0)
        if n <= 0:
            continue
        # Name-only hits can be false positives; require fingerprint in snippets
        # when query was not the fingerprint itself.
        if q == SMS_FINGERPRINT:
            return True
        threads = results.get("threads") or []
        blob = json.dumps(threads)[:8000].lower()
        if "booking cost of $100" in blob or "genie - new zocdoc" in blob:
            return True
        if phone and q.endswith(re.sub(r"\D", "", phone)[-10:]):
            # Phone search hit — still require fee language if messages present
            if "liora" in blob and ("portal" in blob or "$100" in blob or "100" in blob):
                return True
    return False


def ema_portal_active(ema, patient_id: str) -> bool:
    try:
        p = ema.get_patient(patient_id)
    except Exception:
        return False
    if not isinstance(p, dict):
        return False
    # username present ⇒ portal active (runbook)
    return bool(p.get("username"))


# ── Process one candidate ────────────────────────────────────────────────────


def _checkpoint_running(gb, correlation_id: str, steps: list, metadata: dict | None = None) -> None:
    """Best-effort GB running checkpoint with current steps."""
    try:
        gb.report_process(
            TASK_SLUG,
            "running",
            correlation_id=correlation_id,
            steps=steps,
            metadata=metadata,
        )
    except Exception:
        pass


def process_one(
    *,
    appt: dict,
    zoc,
    weave,
    ema,
    gb,
    sms_template: str,
    dry_run: bool,
    force: bool = False,
    ledger: StepLedger | None = None,
) -> str:
    """Process a single booking. Returns status: processed|skipped|error|dry-run.

    force=True skips only the GB completed gate; ledger, booking call-already,
    step_done, and Weave fingerprint still block double call-request / SMS.
    """
    appointment_id = appt.get("appointmentId") or ""
    patient_obj = appt.get("patient") or {}
    first_name = patient_obj.get("firstName") or ""
    last_name = patient_obj.get("lastName") or ""
    patient_name = f"{first_name} {last_name}".strip() or "(unknown)"
    display = _mask_name(patient_name)
    mrn = appt.get("mrn") or appt.get("patientId") or appointment_id or "unknown"
    appt_date = (
        appt.get("appointmentTimeUtc")
        or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    )[:10]
    correlation_id = build_correlation_id(appointment_id, mrn=str(mrn), appt_date=appt_date)
    ledger = ledger or StepLedger()

    # Always load GB prior for step merge; force only ignores completed skip
    prior = None
    try:
        prior = gb_prior_execution(gb, correlation_id)
    except Exception as e:
        if not dry_run:
            raise
        print(f"  WARN {display}: GB prior query failed: {_redact_error(e)}")

    if prior and str(prior.get("status")) == "completed" and not force:
        print(f"  SKIP {display}: already completed ({correlation_id})")
        return "skipped"

    ledger_entry = ledger.get(correlation_id) or {}
    prior_steps = merge_step_lists(
        (prior or {}).get("steps") if prior else None,
        ledger_entry.get("steps"),
    )

    # Booking details (PHI stays local — not printed)
    booking_detail = zoc.get_booking(appointment_id) if appointment_id else {}
    appt_details = (booking_detail or {}).get("data", {}).get("appointmentDetails", {}) or {}
    patient_info = appt_details.get("patient") or {}
    phone = patient_info.get("phoneNumber") or patient_obj.get("phoneNumber") or ""
    email = patient_info.get("email") or patient_obj.get("email") or ""
    request_id = appt_details.get("requestId") or appt.get("requestId")
    actual_first = patient_info.get("firstName") or first_name
    call_from_booking = booking_call_already_requested(appt_details)

    # EMA lookup (best-effort)
    ema_patient_id = None
    portal_active = False
    if last_name or first_name:
        try:
            ema_results = ema.search_patients(
                last_name=last_name or None,
                first_name=first_name or None,
            )
            if ema_results:
                ema_patient_id = ema_results[0].get("id")
                email = email or ema_results[0].get("email") or ""
                if ema_patient_id:
                    portal_active = ema_portal_active(ema, str(ema_patient_id))
        except Exception as e:
            print(f"  WARN {display}: EMA lookup failed: {_redact_error(e)}")

    call_already = (
        step_done(prior_steps, "call", "send_call_request") or call_from_booking
    )
    portal_already = step_done(prior_steps, "portal", "activate_portal", "send_portal")
    sms_already = step_done(prior_steps, "sms", "welcome", "weave", "send_welcome")

    # Weave fingerprint always honored (including --force) to prevent double-SMS
    weave_has_sms = False
    if not sms_already:
        try:
            weave_has_sms = weave_already_sent_genie_sms(
                weave, first_name, last_name, phone or None
            )
        except Exception as e:
            print(f"  WARN {display}: Weave search failed: {_redact_error(e)}")

    if dry_run:
        try:
            sms_body = build_sms_body(sms_template, first_name=actual_first)
        except (ValueError, RuntimeError) as e:
            print(f"  WARN {display}: template invalid: {_redact_error(e)}")
            sms_body = ""
        print(f"  DRY-RUN {display} corr={correlation_id}")
        print(f"    phone={_mask_phone(phone)} email={_mask_email(email)} "
              f"requestId={'yes' if request_id else 'no'} "
              f"ema={'yes' if ema_patient_id else 'no'} portal_active={portal_active}")
        print(f"    prior_status={(prior or {}).get('status') if prior else None} "
              f"ledger_steps={len(ledger_entry.get('steps') or [])} "
              f"call_from_booking={call_from_booking}")
        print(f"    WOULD call_request={'skip-done' if call_already else ('yes' if request_id else 'skip-no-id')}")
        print(f"    WOULD portal={'skip-active' if portal_active or portal_already else ('yes' if ema_patient_id and email else 'skip')}")
        print(f"    WOULD sms={'skip-done' if sms_already or weave_has_sms else ('yes' if phone else 'skip-no-phone')}")
        print(
            f"    sms template_id={SMS_TEMPLATE_ID} template_name={SMS_TEMPLATE_NAME} "
            f"sms_len={len(sms_body)} fingerprint_ok={SMS_FINGERPRINT in sms_body}"
        )
        return "dry-run"

    started_at = datetime.now(timezone.utc).isoformat()
    steps: list[dict] = [
        make_step(1, "Pulled appointment from ZocDoc", "done",
                  f"appointmentId hash={hashlib.sha256(str(appointment_id).encode()).hexdigest()[:12]}")
    ]
    # Seed with any prior terminal steps so failure reports stay complete
    steps = merge_step_lists(prior_steps, steps)
    current_step = "init"

    try:
        try:
            gb.report_process(
                TASK_SLUG,
                "running",
                correlation_id=correlation_id,
                trigger_type="cron",
                trigger_source="zocdoc",
                patient=_patient_gb_payload(str(mrn), patient_name, phone),
                appointment={
                    "id": appointment_id,
                    "date": appt_date,
                    "request_id": str(request_id) if request_id else None,
                },
                steps=steps,
                started_at=started_at,
                metadata={"job": "zocdoc_new_booking", "version": 3},
            )
        except Exception as e:
            # Still attempt work; completion report may recover
            print(f"  WARN {display}: GB running report failed: {_redact_error(e)}")

        # Step 2: Zocdoc call-the-office ($100 fee window) — checkpoint before portal/SMS
        current_step = "call_request"
        if call_already:
            detail = (
                "requestedToCallTimestamp set on booking"
                if call_from_booking and not step_done(prior_steps, "call", "send_call_request")
                else "already done on prior run"
            )
            steps.append(make_step(2, "Sent call office request on ZocDoc", "skipped", detail))
        elif request_id:
            try:
                zoc.send_call_request(str(request_id), reasons=["Other"])
                steps.append(make_step(2, "Sent call office request on ZocDoc", "done",
                                       "requestId present"))
            except Exception as e:
                if call_request_error_is_already_done(e):
                    steps.append(make_step(
                        2, "Sent call office request on ZocDoc", "skipped",
                        f"API already-requested: {_redact_error(e)}",
                    ))
                else:
                    steps.append(make_step(2, "Sent call office request on ZocDoc", "failed",
                                           _redact_error(e)))
                    raise JobStepError(
                        _redact_error(e),
                        step="call_request",
                        correlation_id=correlation_id,
                        next_action=(
                            "check Zocdoc requestId / Kernel auth; do not force-retry "
                            "until ledger/GB shows step 2 done"
                        ),
                        steps=steps,
                    ) from e
        else:
            steps.append(make_step(2, "Sent call office request on ZocDoc", "failed",
                                   "No requestId — cannot send call request"))
            raise JobStepError(
                "missing requestId for call request ($100 fee step)",
                step="call_request",
                correlation_id=correlation_id,
                next_action=(
                    "open booking in Zocdoc; ensure requestId present; "
                    "cannot skip $100 fee step"
                ),
                steps=steps,
            )

        # P1 fee gate: persist step 2 before any portal/SMS side effects
        steps = merge_step_lists(steps)
        try:
            ledger.record(
                correlation_id,
                steps=steps,
                status="running",
                appointment_id=str(appointment_id) if appointment_id else None,
            )
        except Exception as e:
            print(f"  WARN {display}: ledger record after call_request failed: {_redact_error(e)}")
        _checkpoint_running(gb, correlation_id, steps)

        # Step 3: EMA portal activate (omit cellPhone — client enforces)
        current_step = "portal"
        if portal_already or portal_active:
            steps.append(make_step(3, "Activated patient portal in ModMed", "skipped",
                                   "already active or done"))
        elif ema_patient_id and email:
            try:
                # username = email per historical practice
                ema.send_portal_email(str(ema_patient_id), email, email)
                steps.append(make_step(3, "Activated patient portal in ModMed", "done"))
            except Exception as e:
                # Portal failure is non-fatal for fee path; surface but continue to SMS
                steps.append(make_step(3, "Activated patient portal in ModMed", "failed",
                                       _redact_error(e)))
        else:
            steps.append(make_step(3, "Activated patient portal in ModMed", "skipped",
                                   "missing ema patient or email"))

        try:
            ledger.record(
                correlation_id,
                steps=steps,
                status="running",
                appointment_id=str(appointment_id) if appointment_id else None,
            )
        except Exception:
            pass

        # Step 4: Weave SMS (template only)
        current_step = "sms"
        if sms_already or weave_has_sms:
            steps.append(make_step(4, "Sent Genie SMS via Weave", "skipped",
                                   "already messaged"))
        elif phone:
            try:
                body = build_sms_body(sms_template, first_name=actual_first)
                resp = weave.send_message(phone, body)
                meta = {}
                if isinstance(resp, dict):
                    for k in ("smsId", "threadId", "personId"):
                        if resp.get(k):
                            meta[k] = resp[k]
                detail = (
                    f"sms sent template_id={SMS_TEMPLATE_ID} "
                    f"template_name={SMS_TEMPLATE_NAME}"
                )
                if meta:
                    detail += f" ids={list(meta.keys())}"
                steps.append(make_step(4, "Sent Genie SMS via Weave", "done", detail))
                try:
                    ledger.record(
                        correlation_id,
                        steps=steps,
                        status="running",
                        appointment_id=str(appointment_id) if appointment_id else None,
                    )
                except Exception as e:
                    print(f"  WARN {display}: ledger record after sms failed: {_redact_error(e)}")
                # Keep thread ids on GB without phone
                _checkpoint_running(
                    gb, correlation_id, steps,
                    metadata={"weave": meta} if meta else None,
                )
            except JobStepError:
                raise
            except Exception as e:
                steps.append(make_step(4, "Sent Genie SMS via Weave", "failed",
                                       _redact_error(e)))
                raise JobStepError(
                    _redact_error(e),
                    step="sms",
                    correlation_id=correlation_id,
                    next_action=(
                        "check Weave auth / phone on booking; do not resend until "
                        "ledger shows step 4 done or Weave fingerprint present"
                    ),
                    steps=steps,
                ) from e
        else:
            steps.append(make_step(4, "Sent Genie SMS via Weave", "failed", "no phone"))
            raise JobStepError(
                "missing phone for SMS",
                step="sms",
                correlation_id=correlation_id,
                next_action="open booking in Zocdoc; ensure patient phone present",
                steps=steps,
            )

        # Critical path: call_request must be done; SMS done or skipped-as-already
        call_ok = step_done(steps, "call")
        sms_ok = step_done(steps, "sms", "weave", "welcome")
        if not call_ok:
            raise JobStepError(
                "call request step not successful",
                step="call_request",
                correlation_id=correlation_id,
                next_action=(
                    "check Zocdoc requestId / Kernel auth; do not force-retry "
                    "until ledger/GB shows step 2 done"
                ),
                steps=steps,
            )
        if not sms_ok:
            raise JobStepError(
                "sms step not successful",
                step="sms",
                correlation_id=correlation_id,
                next_action="inspect step 4 detail; avoid double-SMS on retry",
                steps=steps,
            )

        current_step = "report_completed"
        completed_at = datetime.now(timezone.utc).isoformat()
        outcome_bits = []
        for s in steps:
            if s.get("status") == "done":
                outcome_bits.append(str(s.get("action")))
        gb.report_process(
            TASK_SLUG,
            "completed",
            correlation_id=correlation_id,
            patient=_patient_gb_payload(str(mrn), patient_name, phone),
            appointment={"id": appointment_id, "date": appt_date},
            steps=steps,
            outcome_summary="; ".join(outcome_bits) or "Processed new Zocdoc patient",
            completed_at=completed_at,
            duration_ms=int(
                (datetime.fromisoformat(completed_at) - datetime.fromisoformat(started_at))
                .total_seconds()
                * 1000
            ),
            metadata={"job": "zocdoc_new_booking", "version": 3},
        )
        try:
            ledger.record(
                correlation_id,
                steps=steps,
                status="completed",
                appointment_id=str(appointment_id) if appointment_id else None,
            )
        except Exception as e:
            print(f"  WARN {display}: ledger record completed failed: {_redact_error(e)}")
        try:
            gb.log_activity(
                "zocdoc_new_patient_processed",
                f"Processed new ZocDoc patient corr={correlation_id}",
                source="zocdoc-cron",
                patient=_patient_gb_payload(str(mrn), patient_name),
                payload={"correlation_id": correlation_id},
            )
        except Exception:
            pass

        print(f"  OK {display} corr={correlation_id}")
        return "processed"

    except JobStepError as e:
        if not e.steps:
            e.steps = list(steps)
        try:
            ledger.record(
                correlation_id,
                steps=e.steps,
                status="failed",
                appointment_id=str(appointment_id) if appointment_id else None,
            )
        except Exception:
            pass
        report_failure(gb, correlation_id, patient_name, str(mrn), e, steps=e.steps)
        raise
    except Exception as e:
        jse = JobStepError(
            _redact_error(e),
            step=current_step,
            correlation_id=correlation_id,
            next_action=(
                "inspect ledger/GB steps; do not force-retry fee/SMS until step status known"
            ),
            steps=steps,
        )
        try:
            ledger.record(
                correlation_id,
                steps=steps,
                status="failed",
                appointment_id=str(appointment_id) if appointment_id else None,
            )
        except Exception:
            pass
        report_failure(gb, correlation_id, patient_name, str(mrn), jse, steps=steps)
        raise jse from e


def report_failure(gb, correlation_id: str, patient_name: str, mrn: str,
                   err: BaseException, steps: list | None = None) -> None:
    msg = _redact_error(err)
    try:
        gb.report_process(
            TASK_SLUG,
            "failed",
            correlation_id=correlation_id,
            patient=_patient_gb_payload(str(mrn), patient_name),
            error_message=msg,
            steps=steps,
            outcome_summary=f"Failed: {msg[:200]}",
        )
        gb.request_feedback(
            f"Zocdoc new booking failed corr={correlation_id}",
            description=msg,
            priority="high",
            patient=_patient_gb_payload(str(mrn), patient_name),
            bot_context={"correlation_id": correlation_id, "steps_count": len(steps or [])},
        )
    except Exception as e:
        print(f"  WARN: could not report failure to GB: {_redact_error(e)}",
              file=sys.stderr)


# ── Main ─────────────────────────────────────────────────────────────────────


def main(
    dry_run: bool = False,
    lookback_minutes: int = DEFAULT_LOOKBACK_MINUTES,
    max_patients: int | None = None,
    force: bool = False,
    skip_lock: bool = False,
    lock_path: str = DEFAULT_LOCK_PATH,
) -> dict:
    """Run the job. Returns summary dict (also printed)."""
    start = time.time()
    lock = JobLock(lock_path)
    if not skip_lock and not dry_run:
        if not lock.acquire():
            summary = {
                "status": "locked",
                "message": "Another zocdoc-new-booking run holds the lock",
                "processed": 0,
                "skipped": 0,
                "errors": 0,
            }
            print(json.dumps(summary))
            return summary

    print(
        f"zocdoc-new-booking start dry_run={dry_run} lookback={lookback_minutes}m "
        f"max={max_patients or 'none'}"
    )

    zoc = weave = ema = gb = None
    sms_template = SMS_TEMPLATE_BODY
    try:
        zoc, weave, ema, gb, sms_template = init_clients(require_gb=not dry_run)
        if gb is None:
            gb = _NullGB()
    except Exception as e:
        err = {"status": "auth_error", "error": _redact_error(e)}
        print(json.dumps(err), file=sys.stderr)
        raise SystemExit(2) from e

    if not dry_run and gb is not None:
        try:
            gb._post("/api/webhooks/register-flow", {
                "task_slug": TASK_SLUG,
                "flow_definition": FLOW_DEFINITION,
                "schedule": {"cron": "*/30 * * * *", "timezone": "America/New_York"},
            })
        except Exception:
            pass
        try:
            gb.log_activity(
                "zocdoc_routine_check",
                "Starting Zocdoc new patient scan",
                source="zocdoc-cron",
            )
        except Exception:
            pass
    else:
        print("DRY-RUN — no call-request, SMS, portal, or mutating GB reports\n")

    # Fetch bookings (paginate a bit so lookback window is covered)
    all_appts: list[dict] = []
    bookings_data: dict = {}
    try:
        for page in range(1, 4):
            chunk = zoc.list_bookings(page_number=page, page_size=25)
            bookings_data = chunk
            rows = (
                chunk.get("data", {})
                .get("appointments", {})
                .get("appointments", [])
            ) or []
            all_appts.extend(rows)
            pages = (
                chunk.get("data", {})
                .get("appointments", {})
                .get("pagesCount")
            )
            if not rows or (pages is not None and page >= int(pages)):
                break
        # Re-wrap for extract_candidates
        bookings_data = {
            "data": {"appointments": {"appointments": all_appts}},
        }
        candidates = extract_candidates(bookings_data, lookback_minutes=lookback_minutes)
    except Exception as e:
        print(f"ERROR listing bookings: {_redact_error(e)}", file=sys.stderr)
        if gb and not dry_run:
            try:
                gb.request_feedback(
                    "Zocdoc new booking scan failed",
                    description=_redact_error(e),
                    priority="high",
                )
            except Exception:
                pass
        raise SystemExit(3) from e

    if max_patients is not None:
        candidates = candidates[: max(0, max_patients)]

    print(f"Found {len(candidates)} new patient candidate(s)")

    counts = {"processed": 0, "skipped": 0, "errors": 0, "dry_run": 0}
    correlation_ids: list[str] = []
    ledger = StepLedger()

    for appt in candidates:
        appointment_id = appt.get("appointmentId") or ""
        patient_obj = appt.get("patient") or {}
        first_name = patient_obj.get("firstName") or ""
        last_name = patient_obj.get("lastName") or ""
        patient_name = f"{first_name} {last_name}".strip() or "(unknown)"
        mrn = appt.get("mrn") or appt.get("patientId") or appointment_id or "unknown"
        appt_date = (
            appt.get("appointmentTimeUtc")
            or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        )[:10]
        correlation_id = build_correlation_id(
            appointment_id, mrn=str(mrn), appt_date=appt_date
        )
        correlation_ids.append(correlation_id)

        try:
            # Dry-run still queries GB gates via process_one
            result = process_one(
                appt=appt,
                zoc=zoc,
                weave=weave,
                ema=ema,
                gb=gb,
                sms_template=sms_template,
                dry_run=dry_run,
                force=force,
                ledger=ledger,
            )
            if result == "processed":
                counts["processed"] += 1
            elif result == "dry-run":
                counts["dry_run"] += 1
            else:
                counts["skipped"] += 1
        except JobStepError as e:
            # process_one already reported failure with in-flight steps
            counts["errors"] += 1
            print(
                f"  ERROR {_mask_name(patient_name)} corr={correlation_id}: "
                f"{_redact_error(e)}",
                file=sys.stderr,
            )
            continue
        except Exception as e:
            counts["errors"] += 1
            print(
                f"  ERROR {_mask_name(patient_name)} corr={correlation_id}: "
                f"{_redact_error(e)}",
                file=sys.stderr,
            )
            if not dry_run and gb is not None:
                # Early failure before process_one attached steps
                report_failure(gb, correlation_id, patient_name, str(mrn), e)
            continue

        # Gentle pacing between patients
        if not dry_run:
            time.sleep(1.0)

    elapsed = round(time.time() - start, 1)
    summary = {
        "status": "ok",
        "dry_run": dry_run,
        "candidates": len(candidates),
        "processed": counts["processed"],
        "skipped": counts["skipped"],
        "dry_run_listed": counts["dry_run"],
        "errors": counts["errors"],
        "elapsed_s": elapsed,
        "correlation_ids": correlation_ids[:50],
        "sms_template_fingerprint_ok": SMS_FINGERPRINT in sms_template,
    }
    print(
        f"Scan complete: {counts['processed']} processed, "
        f"{counts['skipped']} skipped, {counts['dry_run']} dry-run, "
        f"{counts['errors']} errors ({elapsed}s)"
    )
    print(json.dumps({k: v for k, v in summary.items() if k != "correlation_ids"}))

    try:
        if zoc is not None:
            zoc.close()
    except Exception:
        pass
    if not skip_lock:
        lock.release()
    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Zocdoc new-patient booking job")
    p.add_argument("--dry-run", action="store_true",
                   help="Discover + plan only; no call-request/SMS/portal/GB mutate")
    p.add_argument("--lookback-minutes", type=int, default=DEFAULT_LOOKBACK_MINUTES,
                   help=f"Booking age window (default {DEFAULT_LOOKBACK_MINUTES})")
    p.add_argument("--max-patients", type=int, default=None,
                   help="Cap candidates processed this run")
    p.add_argument("--force", action="store_true",
                   help="Ignore completed GB gate only; still honors ledger, "
                        "booking call-already, step_done, and Weave SMS fingerprint")
    p.add_argument("--skip-lock", action="store_true",
                   help="Do not take exclusive file lock")
    p.add_argument("--lock-path", default=DEFAULT_LOCK_PATH)
    return p.parse_args(argv)


if __name__ == "__main__":
    # Support legacy bare --lookback-minutes=N as well as argparse
    argv = sys.argv[1:]
    args = _parse_args(argv)
    main(
        dry_run=args.dry_run,
        lookback_minutes=args.lookback_minutes,
        max_patients=args.max_patients,
        force=args.force,
        skip_lock=args.skip_lock,
        lock_path=args.lock_path,
    )
