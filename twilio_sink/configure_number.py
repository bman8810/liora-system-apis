#!/usr/bin/env python3
"""Configure Twilio DID voice webhooks for sink C + optional probe call.

Reads secrets from env only:
  TWILIO_ACCOUNT_SID
  TWILIO_API_KEY_SID + TWILIO_API_KEY_SECRET  (or TWILIO_AUTH_TOKEN)
  TWILIO_PHONE_NUMBER
  TWILIO_SINK_PUBLIC_BASE   e.g. https://xxxx.trycloudflare.com

Usage:
  python -m twilio_sink.configure_number
  python -m twilio_sink.configure_number --probe --to +13302067819
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Allow running from repo root without install
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from twilio_sink.config import settings  # noqa: E402
from twilio_sink import twilio_rest  # noqa: E402


def _public_base() -> str:
    base = settings.public_base() or os.environ.get("TWILIO_SINK_PUBLIC_BASE", "")
    base = base.rstrip("/")
    if not base.startswith("https://"):
        raise SystemExit("TWILIO_SINK_PUBLIC_BASE must be https://… (Twilio requires HTTPS)")
    return base


def configure_number(base: str) -> dict:
    numbers = twilio_rest.list_incoming_numbers()
    if not numbers:
        raise SystemExit("No incoming phone numbers on account")
    want = (settings.twilio_phone_number or "").strip()
    match = None
    for n in numbers:
        if not want or n.get("phone_number") == want:
            match = n
            break
    if not match:
        raise SystemExit(f"Phone {want!r} not found on account")
    sid = match["sid"]
    voice_url = f"{base}/voice/answer"
    status_url = f"{base}/voice/status"
    updated = twilio_rest.update_incoming_number(
        sid,
        VoiceUrl=voice_url,
        VoiceMethod="POST",
        StatusCallback=status_url,
        StatusCallbackMethod="POST",
        # Friendly label only — no secrets
        FriendlyName="Liora sink C",
    )
    return {
        "phone_number": updated.get("phone_number") or match.get("phone_number"),
        "phone_sid": sid,
        "voice_url": updated.get("voice_url"),
        "voice_method": updated.get("voice_method"),
        "status_callback": updated.get("status_callback"),
    }


def probe_call(base: str, to: str) -> dict:
    """Outbound call From our DID → to; Twilio fetches Url TwiML (same answer webhook)."""
    frm = settings.twilio_phone_number
    if not frm:
        raise SystemExit("TWILIO_PHONE_NUMBER required for probe")
    if not to:
        raise SystemExit("--to required for probe")
    call = twilio_rest.create_call(
        To=to,
        From=frm,
        Url=f"{base}/voice/answer",
        Method="POST",
        StatusCallback=f"{base}/voice/status",
        StatusCallbackMethod="POST",
        StatusCallbackEvent="initiated ringing answered completed",
        Record=False,  # TwiML Record handles recording on answered leg
        Timeout=30,
    )
    return {
        "call_sid": call.get("sid"),
        "status": call.get("status"),
        "to": call.get("to"),
        "from": call.get("from"),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Configure Twilio sink C webhooks")
    p.add_argument("--probe", action="store_true", help="Place outbound probe call using answer URL")
    p.add_argument("--to", default=os.environ.get("TWILIO_PROBE_TO", "+13302067819"))
    p.add_argument("--skip-configure", action="store_true")
    p.add_argument("--wait", type=int, default=45, help="Seconds to poll call after probe")
    args = p.parse_args(argv)

    if not settings.has_rest_auth():
        raise SystemExit("Twilio REST auth missing in env")

    base = _public_base()
    out: dict = {"public_base": base}

    if not args.skip_configure:
        out["number"] = configure_number(base)
        print(json.dumps({"configured": out["number"]}, indent=2))

    if args.probe:
        out["probe"] = probe_call(base, args.to)
        print(json.dumps({"probe_started": out["probe"]}, indent=2))
        call_sid = out["probe"]["call_sid"]
        deadline = time.time() + max(5, args.wait)
        last = {}
        while time.time() < deadline:
            last = twilio_rest.get_call(call_sid)
            st = last.get("status")
            print(f"poll {call_sid} status={st} duration={last.get('duration')}")
            if st in ("completed", "busy", "failed", "no-answer", "canceled"):
                break
            time.sleep(3)
        recs = twilio_rest.list_recordings(call_sid)
        out["final_status"] = {
            "status": last.get("status"),
            "duration": last.get("duration"),
            "start_time": last.get("start_time"),
            "end_time": last.get("end_time"),
            "recordings": [
                {
                    "sid": r.get("sid"),
                    "duration": r.get("duration"),
                    "status": r.get("status"),
                    # media URL path only — fetch with Basic auth
                    "uri": r.get("uri"),
                }
                for r in recs
            ],
        }
        print(json.dumps({"probe_final": out["final_status"]}, indent=2))

    # Write non-secret summary next to artifacts
    summary_path = os.path.join(settings.ensure_artifact_dir(), "last_configure.json")
    with open(summary_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"summary_written={summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
