"""Smoke-test SIP REGISTER for Barric softphone 7002.

Usage (from repo root, with venv):
  export LIORA_CREDENTIALS_DIR=/path/to/.credentials
  export LIORA_SECRETS_DIR=/path/to/.secrets   # optional sip_7002.env
  export WEAVE_TOKEN=...                      # or weave_token.json in credentials dir
  python -m voice_agent.register_smoke

Does NOT place a call. Prints non-secret registration proof JSON.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .auth import check_registration, fetch_sip_credentials, get_session
from .sip_client import SipClient

log = logging.getLogger("voice_agent.register_smoke")


def _load_token() -> str:
    token = os.environ.get("WEAVE_TOKEN") or config.WEAVE_TOKEN
    if token:
        return token
    cred_dir = Path(
        os.environ.get(
            "LIORA_CREDENTIALS_DIR",
            Path.home() / ".liora" / "credentials",
        )
    )
    path = cred_dir / "weave_token.json"
    if path.exists():
        data = json.loads(path.read_text())
        tok = data.get("token") or data.get("access_token")
        if tok:
            return tok
    raise SystemExit(
        "No WEAVE_TOKEN. Set env or place weave_token.json under LIORA_CREDENTIALS_DIR."
    )


async def _register(creds: dict) -> tuple[bool, dict | None]:
    client = SipClient(
        username=creds["username"],
        password=creds["password"],
        domain=creds["domain"],
        proxy=creds["proxy"],
    )
    await client.connect()
    task = asyncio.create_task(client.run())
    ok = await client.register(timeout=20.0)
    await asyncio.sleep(1.0)
    await client.close()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return ok, None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )
    token = _load_token()
    session = get_session(token)
    creds = fetch_sip_credentials(session)
    if int(creds["extension"]) != int(config.SIP_EXTENSION):
        log.error(
            "Extension mismatch: fetched %s expected %s",
            creds["extension"],
            config.SIP_EXTENSION,
        )
        return 2

    pre = check_registration(session, creds["sip_profile_id"])
    ok, _ = asyncio.run(_register(creds))
    post = check_registration(session, creds["sip_profile_id"])

    report = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "pass": bool(ok and (post.get("registration") or {}).get("active")),
        "extension": creds["extension"],
        "softphone_name": creds.get("softphone_name"),
        "softphone_id": creds.get("softphone_id"),
        "sip_profile_id": creds["sip_profile_id"],
        "username": creds["username"],
        "domain": creds["domain"],
        "proxy": creds["proxy"],
        "sip_aor": f"sip:{creds['username']}@{creds['domain']}",
        "ws_url": f"wss://{creds['proxy']}",
        "transport": "WSS",
        "subprotocol": "sip",
        "codecs_lab": ["PCMU/8000", "PCMA/8000"],
        "dialstring_b_to_c_e164": config.DIALSTRING_B_TO_C_E164,
        "dialstring_b_to_c_10digit": config.DIALSTRING_B_TO_C_10DIGIT,
        "dialstring_b_to_c_sip": config.DIALSTRING_B_TO_C_SIP,
        "register_ok": ok,
        "registration_api_before_active": (pre.get("registration") or {}).get("active"),
        "registration_api_after": post.get("registration"),
    }
    print(json.dumps(report, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
