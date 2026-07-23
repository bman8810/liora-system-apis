"""Weave authentication and SIP credential fetching."""

from __future__ import annotations

import os
from typing import Any

import requests

from . import config


def get_session(token: str) -> requests.Session:
    """Create a requests.Session with Weave auth headers."""
    s = requests.Session()
    s.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Location-Id": config.LOCATION_ID,
            "Content-Type": "application/json",
        }
    )
    return s


def _pick_sip_profile(
    data: dict[str, Any],
    *,
    extension: int | None = None,
    sip_profile_id: str | None = None,
    softphone_id: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    """Select softphone + sip profile. Prefer Barric 7002 / configured IDs."""
    proxy = data["proxy"]
    softphones = data.get("softphones") or []
    if not softphones:
        raise RuntimeError("No softphones in /phone/softphones/settings response")

    extension = (
        extension
        if extension is not None
        else int(os.environ.get("WEAVE_SIP_EXTENSION", config.SIP_EXTENSION))
    )
    sip_profile_id = sip_profile_id or os.environ.get(
        "WEAVE_SIP_PROFILE_ID", config.SIP_PROFILE_ID
    )
    softphone_id = softphone_id or os.environ.get(
        "WEAVE_SOFTPHONE_ID", config.SOFTPHONE_ID
    )

    # 1) Match configured sip profile id
    for sp in softphones:
        for sip in sp.get("sipProfiles") or []:
            if sip.get("id") == sip_profile_id:
                return sp, sip, proxy

    # 2) Match softphone id + first profile
    for sp in softphones:
        if sp.get("id") == softphone_id and (sp.get("sipProfiles") or []):
            return sp, sp["sipProfiles"][0], proxy

    # 3) Match extension number (Barric 7002)
    for sp in softphones:
        for sip in sp.get("sipProfiles") or []:
            if sip.get("extensionNumber") == extension:
                return sp, sip, proxy

    # 4) Name contains Barric / 7002
    for sp in softphones:
        name = (sp.get("name") or "").lower()
        if "barric" in name or "7002" in name:
            sips = sp.get("sipProfiles") or []
            if sips:
                return sp, sips[0], proxy

    # Fallback: first softphone/profile (warn via caller logs)
    sp0 = softphones[0]
    sips = sp0.get("sipProfiles") or []
    if not sips:
        raise RuntimeError(f"Softphone {sp0.get('id')} has no sipProfiles")
    return sp0, sips[0], proxy


def fetch_sip_credentials(session: requests.Session) -> dict:
    """Fetch SIP credentials from softphone settings API.

    Returns dict with keys: username, password, domain, proxy, extension,
    sip_profile_id, softphone_id, softphone_name.
    Prefers Barric extension 7002 / WEAVE_SIP_* env, not Genie 7018.
    """
    r = session.get(
        f"{config.API_BASE}/phone/softphones/settings",
        params={"locationIds": config.LOCATION_ID},
    )
    r.raise_for_status()
    data = r.json()

    softphone, sip_profile, proxy = _pick_sip_profile(data)
    return {
        "username": sip_profile["username"],
        "password": sip_profile["password"],
        "domain": sip_profile["domain"],
        "proxy": proxy,
        "extension": sip_profile["extensionNumber"],
        "sip_profile_id": sip_profile["id"],
        "softphone_id": softphone.get("id"),
        "softphone_name": softphone.get("name"),
    }


def initiate_dial(session: requests.Session, destination: str) -> dict:
    """Initiate an outbound call via the dial API.

    Safety: only dials ALLOWED_DIAL_PHONES.
    Uses configured Barric SIP profile id (7002), not Genie hardcode.
    """
    phone = (
        destination.replace("-", "")
        .replace("(", "")
        .replace(")", "")
        .replace(" ", "")
        .replace("+", "")
    )
    if phone.startswith("1") and len(phone) == 11:
        phone = phone[1:]

    e164 = f"+1{phone}"
    if e164 not in config.ALLOWED_DIAL_PHONES:
        raise ValueError(
            f"SAFETY: Refusing to dial {e164}. "
            f"Only {config.ALLOWED_DIAL_PHONES} are allowed."
        )

    sip_profile_id = os.environ.get("WEAVE_SIP_PROFILE_ID", config.SIP_PROFILE_ID)

    payload = {
        "fromName": config.FROM_NAME,
        "fromNumber": config.FROM_NUMBER,
        "toNumber": phone,
        "sipProfileId": sip_profile_id,
    }

    r = session.post(
        f"{config.API_BASE}/phone-exp/phone-call/v1/dial", json=payload
    )
    r.raise_for_status()
    return r.json() if r.text else {"status": r.status_code}


def check_registration(
    session: requests.Session, sip_profile_id: str | None = None
) -> dict:
    """Check SIP profile registration status."""
    pid = sip_profile_id or os.environ.get(
        "WEAVE_SIP_PROFILE_ID", config.SIP_PROFILE_ID
    )
    r = session.get(
        f"{config.API_BASE}/phone/sip-profiles/v1/{pid}/registration",
    )
    r.raise_for_status()
    return r.json()
