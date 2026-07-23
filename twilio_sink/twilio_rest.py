"""Minimal Twilio REST helpers (stdlib). Secrets stay in env."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from twilio_sink.config import settings


def _auth_header() -> str:
    user, pw = settings.rest_basic_auth()
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    return f"Basic {token}"


def _account_sid() -> str:
    sid = settings.twilio_account_sid
    if not sid:
        raise RuntimeError("TWILIO_ACCOUNT_SID missing")
    return sid


def request(
    method: str,
    path: str,
    form: dict[str, Any] | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """path is under /2010-04-01/Accounts/{sid}/… or absolute https URL."""
    if path.startswith("http"):
        url = path
    else:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{_account_sid()}/{path.lstrip('/')}"
    data = None
    headers = {"Authorization": _auth_header(), "Accept": "application/json"}
    if form is not None:
        data = urllib.parse.urlencode({k: v for k, v in form.items() if v is not None}).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")
        raise RuntimeError(f"Twilio REST {method} {url} -> {e.code}: {err_body[:500]}") from e


def list_incoming_numbers() -> list[dict[str, Any]]:
    data = request("GET", "IncomingPhoneNumbers.json")
    return list(data.get("incoming_phone_numbers") or [])


def update_incoming_number(phone_sid: str, **params: Any) -> dict[str, Any]:
    return request("POST", f"IncomingPhoneNumbers/{phone_sid}.json", form=params)


def create_call(**params: Any) -> dict[str, Any]:
    return request("POST", "Calls.json", form=params)


def get_call(call_sid: str) -> dict[str, Any]:
    return request("GET", f"Calls/{call_sid}.json")


def list_recordings(call_sid: str | None = None) -> list[dict[str, Any]]:
    if call_sid:
        data = request("GET", f"Calls/{call_sid}/Recordings.json")
    else:
        data = request("GET", "Recordings.json")
    return list(data.get("recordings") or [])
