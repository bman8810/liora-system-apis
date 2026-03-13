"""Weave authentication and SIP credential fetching."""

import requests
from . import config


def get_session(token: str) -> requests.Session:
    """Create a requests.Session with Weave auth headers."""
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Location-Id": config.LOCATION_ID,
        "Content-Type": "application/json",
    })
    return s


def fetch_sip_credentials(session: requests.Session) -> dict:
    """Fetch SIP credentials from softphone settings API.

    Returns dict with keys: username, password, domain, proxy, extension.
    """
    r = session.get(
        f"{config.API_BASE}/phone/softphones/settings",
        params={"locationIds": config.LOCATION_ID},
    )
    r.raise_for_status()
    data = r.json()

    proxy = data["proxy"]
    softphone = data["softphones"][0]
    sip_profile = softphone["sipProfiles"][0]

    return {
        "username": sip_profile["username"],
        "password": sip_profile["password"],
        "domain": sip_profile["domain"],
        "proxy": proxy,
        "extension": sip_profile["extensionNumber"],
        "sip_profile_id": sip_profile["id"],
    }


def initiate_dial(session: requests.Session, destination: str) -> dict:
    """Initiate an outbound call via the dial API.

    Safety: only dials ALLOWED_DIAL_PHONE.
    """
    phone = destination.replace("-", "").replace("(", "").replace(")", "").replace(" ", "").replace("+", "")
    if phone.startswith("1") and len(phone) == 11:
        phone = phone[1:]

    e164 = f"+1{phone}"
    if e164 not in config.ALLOWED_DIAL_PHONES:
        raise ValueError(
            f"SAFETY: Refusing to dial {e164}. "
            f"Only {config.ALLOWED_DIAL_PHONES} are allowed."
        )

    payload = {
        "fromName": config.FROM_NAME,
        "fromNumber": config.FROM_NUMBER,
        "toNumber": phone,
        "sipProfileId": config.SIP_PROFILE_ID,
    }

    r = session.post(f"{config.API_BASE}/phone-exp/phone-call/v1/dial", json=payload)
    r.raise_for_status()
    return r.json() if r.text else {"status": r.status_code}


def check_registration(session: requests.Session) -> dict:
    """Check SIP profile registration status."""
    r = session.get(
        f"{config.API_BASE}/phone/sip-profiles/v1/{config.SIP_PROFILE_ID}/registration",
    )
    r.raise_for_status()
    return r.json()
