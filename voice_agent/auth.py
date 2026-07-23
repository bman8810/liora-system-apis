"""Weave authentication and SIP credential fetching for voice product B."""

import logging

import requests

from . import config

logger = logging.getLogger(__name__)


def get_session(token: str) -> requests.Session:
    """Create a requests.Session with Weave auth headers."""
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Location-Id": config.LOCATION_ID,
        "Content-Type": "application/json",
    })
    return s


def _pick_softphone(data: dict) -> tuple[dict, dict]:
    """Select Barric softphone (target ext) from softphones/settings payload."""
    softphones = data.get("softphones") or []
    if not softphones:
        raise RuntimeError("No softphones in Weave softphones/settings response")

    target_ext = config.TARGET_SOFTPHONE_EXTENSION
    chosen = None
    sip_profile = None

    for sp in softphones:
        for prof in sp.get("sipProfiles") or []:
            if int(prof.get("extensionNumber") or 0) == target_ext:
                chosen, sip_profile = sp, prof
                break
        if chosen:
            break

    if not chosen or not sip_profile:
        # Fallback: first softphone's first profile (usually the logged-in user's)
        chosen = softphones[0]
        profiles = chosen.get("sipProfiles") or []
        if not profiles:
            raise RuntimeError("Softphone has no sipProfiles")
        sip_profile = profiles[0]
        logger.warning(
            "Target ext %s not found; using first profile ext %s",
            target_ext,
            sip_profile.get("extensionNumber"),
        )

    return chosen, sip_profile


def apply_softphone_to_config(softphone: dict, sip_profile: dict) -> None:
    """Update module config with live softphone/SIP IDs (no secrets stored)."""
    config.SOFTPHONE_ID = softphone["id"]
    config.SIP_PROFILE_ID = sip_profile["id"]
    config.SIP_USERNAME = sip_profile["username"]
    config.SIP_DOMAIN = sip_profile.get("domain") or config.SIP_DOMAIN
    config.SIP_EXTENSION = int(sip_profile.get("extensionNumber") or config.SIP_EXTENSION)


def fetch_sip_credentials(session: requests.Session) -> dict:
    """Fetch SIP credentials from softphone settings API.

    Prefers TARGET_SOFTPHONE_EXTENSION (Barric 7002). Applies IDs to config
    so dial/registration use the same profile.

    Returns dict with keys: username, password, domain, proxy, extension,
    sip_profile_id, softphone_id.
    """
    r = session.get(
        f"{config.API_BASE}/phone/softphones/settings",
        params={"locationIds": config.LOCATION_ID},
    )
    r.raise_for_status()
    data = r.json()

    proxy = data["proxy"]
    softphone, sip_profile = _pick_softphone(data)
    apply_softphone_to_config(softphone, sip_profile)

    return {
        "username": sip_profile["username"],
        "password": sip_profile["password"],
        "domain": sip_profile["domain"],
        "proxy": proxy,
        "extension": sip_profile["extensionNumber"],
        "sip_profile_id": sip_profile["id"],
        "softphone_id": softphone["id"],
    }


def _normalize_e164(destination: str) -> tuple[str, str]:
    """Return (10-digit national, E.164 +1…) for US numbers."""
    phone = (
        destination.replace("-", "")
        .replace("(", "")
        .replace(")", "")
        .replace(" ", "")
        .replace("+", "")
    )
    if phone.startswith("1") and len(phone) == 11:
        phone = phone[1:]
    e164 = f"+1{phone}" if len(phone) == 10 else f"+{phone}"
    return phone, e164


def initiate_dial(session: requests.Session, destination: str) -> dict:
    """Initiate an outbound call via the dial API.

    Safety: only dials ALLOWED_DIAL_PHONES. Uses Barric SIP profile + Main Line CLI.
    """
    phone, e164 = _normalize_e164(destination)

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

    logger.info(
        "Dial request to=%s from=%s (%s) sipProfileId=%s",
        phone,
        config.FROM_NUMBER,
        config.FROM_NAME,
        config.SIP_PROFILE_ID,
    )

    r = session.post(f"{config.API_BASE}/phone-exp/phone-call/v1/dial", json=payload)
    r.raise_for_status()
    return r.json() if r.text else {"status": r.status_code}


def check_registration(session: requests.Session) -> dict:
    """Check SIP profile registration status for the active (Barric) profile."""
    r = session.get(
        f"{config.API_BASE}/phone/sip-profiles/v1/{config.SIP_PROFILE_ID}/registration",
    )
    r.raise_for_status()
    return r.json()


def verify_voice_b_binding(session: requests.Session) -> dict:
    """Read-only check that voice B is bound to lab DID + outbound CLI.

    Does not place a call. Returns evidence dict for ops notes.
    """
    creds = fetch_sip_credentials(session)
    # Drop password from evidence
    safe_creds = {k: v for k, v in creds.items() if k != "password"}

    reg = None
    reg_err = None
    try:
        reg = check_registration(session)
    except Exception as e:
        reg_err = str(e)

    numbers = session.get(
        f"{config.API_BASE}/phone-exp/phone-numbers/user-accessible",
        params={"locationIds": config.LOCATION_ID},
    )
    numbers.raise_for_status()
    phone_numbers = numbers.json().get("phoneNumbers") or []
    weave_nationals = [
        str(p.get("phoneNumber", {}).get("nationalNumber") or "")
        for p in phone_numbers
    ]
    from_ok = config.FROM_NUMBER in weave_nationals
    from_meta = next(
        (
            p
            for p in phone_numbers
            if str(p.get("phoneNumber", {}).get("nationalNumber")) == config.FROM_NUMBER
        ),
        None,
    )

    sink = config.TWILIO_SINK_DID
    sink_digits = sink.replace("+", "").lstrip("1") if sink else ""
    sink_on_weave = sink_digits in weave_nationals

    return {
        "product": "voice_B",
        "softphone": {
            "id": safe_creds.get("softphone_id"),
            "extension": safe_creds.get("extension"),
            "sip_profile_id": safe_creds.get("sip_profile_id"),
            "username": safe_creds.get("username"),
            "domain": safe_creds.get("domain"),
        },
        "registration": reg,
        "registration_error": reg_err,
        "outbound_cli": {
            "from_number": config.FROM_NUMBER,
            "from_name": config.FROM_NAME,
            "on_weave_accessible": from_ok,
            "caller_id_enabled": (
                (from_meta or {})
                .get("capabilities", {})
                .get("voiceCapabilities", {})
                .get("outboundCallerIdEnabled")
            ),
            "display_name": (from_meta or {}).get("displayName"),
        },
        "lab_sink_did": {
            "e164": sink,
            "in_allowlist": sink in config.ALLOWED_DIAL_PHONES,
            "hosted_on_weave": sink_on_weave,
            "role": (
                "Twilio sink C far-end for B→C lab; not a Weave-hosted DID. "
                "Inbound answer is Twilio webhook (sink C), not Weave softphone."
            ),
        },
        "allowed_dial_phones": sorted(config.ALLOWED_DIAL_PHONES),
        "sip_profile_is_barric_7002": int(safe_creds.get("extension") or 0) == 7002,
        "sip_profile_is_not_genie": safe_creds.get("sip_profile_id")
        != config.SIP_PROFILE_ID_GENIE,
    }
