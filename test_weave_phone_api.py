"""
Test Weave Phone/Calling API client — call history, voicemail, softphone, dial.
Reuses auth from test_weave_api.py (Playwright browser login or --token flag).

Usage:
    python3 test_weave_phone_api.py                    # manual login, run all tests
    python3 test_weave_phone_api.py --token <jwt>      # skip login, use existing token
    python3 test_weave_phone_api.py --dial              # also test dial to Barric Reed (PLACES A REAL CALL)

SAFETY: Dial only goes to 330-206-7819 (Barric Reed) — hardcoded validation.
"""
import json
import uuid
import argparse
import requests

# --- Constants ---
API_BASE = "https://api.weaveconnect.com"
LOCATION_ID = "d8508d79-c71c-4678-b139-eaedb19c2159"
TENANT_ID = "1cdad4ca-9dbe-45f2-8263-c998c1dfec98"
USER_ID = "8b835d4b-d6b3-4e81-a204-6ac39835ba2b"
SOFTPHONE_ID = "dd2b2484-f5f0-43d2-8029-9a140f958fed"
SIP_PROFILE_ID = "c6d657dc-fbdd-47bd-b6e6-bc055dcd3346"
VOICEMAIL_BOX_ID = "97db8842-a469-4d87-8371-a08bd923bd9d"

# Safety: only allow dialing to this number
ALLOWED_DIAL_PHONE = "+13302067819"  # Barric Reed


def get_session(token: str) -> requests.Session:
    """Create a requests.Session with Weave auth headers."""
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Location-Id": LOCATION_ID,
        "Content-Type": "application/json",
    })
    return s


def login_browser() -> str:
    """Login to Weave via Playwright and return the API JWT token."""
    from playwright.sync_api import sync_playwright
    import sys

    print("[1] Launching visible browser...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context()
        page = context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        print("[2] Navigating to Weave sign-in...")
        page.goto("https://app.getweave.com/sign-in", wait_until="networkidle")

        print("[3] Please log in manually in the browser window...")
        print("    Waiting for redirect to dashboard...")
        page.wait_for_url("**/home/**", timeout=120000)
        print(f"    Login successful! URL: {page.url}")

        print("[4] Extracting token from localStorage...")
        token = page.evaluate("localStorage.getItem('token')")
        if not token:
            print("    ERROR: No token found in localStorage!")
            browser.close()
            sys.exit(1)

        print(f"    Got token: {token[:40]}...")
        browser.close()
        return token


# --- Call Records (History) ---
def list_call_records(session: requests.Session, page_size: int = 25) -> dict:
    """List call history records."""
    r = session.get(
        f"{API_BASE}/phone-exp/phone-records/v1/call-records",
        params={"locationIds": LOCATION_ID, "pageSize": str(page_size)},
    )
    r.raise_for_status()
    return r.json()


def list_hydrated_call_records(session: requests.Session, page_size: int = 10) -> dict:
    """List call history records enriched with person data."""
    r = session.get(
        f"{API_BASE}/phone-exp/phone-records/v1/hydrated-call-records",
        params={"locationIds": LOCATION_ID, "pageSize": str(page_size)},
    )
    r.raise_for_status()
    return r.json()


def get_call_records_by_person(session: requests.Session, person_ids: list) -> dict:
    """Get call records for specific person IDs."""
    params = {"locationIds": LOCATION_ID}
    for pid in person_ids:
        params.setdefault("personIds", [])
    r = session.get(
        f"{API_BASE}/phone-exp/phone-records/v1/call-records-by-person-ids",
        params={"locationIds": LOCATION_ID, "personIds": person_ids},
    )
    r.raise_for_status()
    return r.json()


# --- Voicemail ---
def list_voicemails(session: requests.Session, page_size: int = 25) -> dict:
    """List voicemails (hydrated with person data)."""
    r = session.get(
        f"{API_BASE}/phone-exp/phone-records/v1/voicemails",
        params={"locationIds": LOCATION_ID, "pageSize": str(page_size)},
    )
    r.raise_for_status()
    return r.json()


def list_voicemail_messages(session: requests.Session, page_size: int = 25) -> dict:
    """List raw voicemail messages."""
    r = session.get(
        f"{API_BASE}/phone-exp/phone-records/v1/voicemail-messages",
        params={"locationIds": LOCATION_ID, "pageSize": str(page_size)},
    )
    r.raise_for_status()
    return r.json()


def count_unread_voicemails(session: requests.Session) -> dict:
    """Count unread voicemails per mailbox."""
    r = session.get(
        f"{API_BASE}/phone-exp/phone-records/v1/count-unread-voicemails",
        params={"locationIds": LOCATION_ID},
    )
    r.raise_for_status()
    return r.json()


def list_voicemail_boxes(session: requests.Session) -> dict:
    """List voicemail boxes."""
    r = session.get(
        f"{API_BASE}/phone-exp/phone-records/v1/voicemail-mailboxes",
        params={"locationIds": LOCATION_ID},
    )
    r.raise_for_status()
    return r.json()


# --- Softphone & SIP ---
def get_softphone_settings(session: requests.Session) -> dict:
    """Get softphone configuration including SIP credentials."""
    r = session.get(
        f"{API_BASE}/phone/softphones/settings",
        params={"locationIds": LOCATION_ID},
    )
    r.raise_for_status()
    return r.json()


def list_sip_profiles(session: requests.Session) -> dict:
    """List SIP profiles for the tenant."""
    r = session.get(
        f"{API_BASE}/phone/sip-profiles/v1",
        params={"tenantId": TENANT_ID},
    )
    r.raise_for_status()
    return r.json()


def get_tenants(session: requests.Session) -> dict:
    """Get tenant info."""
    r = session.get(
        f"{API_BASE}/phone/tenant/tenants",
        params={"orgId": LOCATION_ID},
    )
    r.raise_for_status()
    return r.json()


# --- Call Initiation ---
def dial(session: requests.Session, destination: str) -> dict:
    """
    Initiate an outbound call via softphone (click-to-call).
    Server handles SIP signaling — no browser WebSocket needed.
    SAFETY: Will only dial ALLOWED_DIAL_PHONE.
    """
    # Normalize to digits only (no +, no -, no spaces)
    phone = destination.replace("-", "").replace("(", "").replace(")", "").replace(" ", "").replace("+", "")
    if phone.startswith("1") and len(phone) == 11:
        phone = phone[1:]  # strip country code for 10-digit format

    e164 = f"+1{phone}"
    if e164 != ALLOWED_DIAL_PHONE:
        raise ValueError(
            f"SAFETY: Refusing to dial {e164}. "
            f"Only {ALLOWED_DIAL_PHONE} (Barric Reed) is allowed."
        )

    payload = {
        "fromName": "Liora Dermatology & Aesthetics",
        "fromNumber": "2124334569",
        "toNumber": phone,
        "sipProfileId": SIP_PROFILE_ID,
    }

    r = session.post(f"{API_BASE}/phone-exp/phone-call/v1/dial", json=payload)
    r.raise_for_status()
    return r.json() if r.text else {"status": r.status_code}


# --- Call Queues ---
def list_call_queues(session: requests.Session) -> dict:
    """List call queues."""
    r = session.post(
        f"{API_BASE}/phone-exp/phone-call/v1/call-queues",
        json={"locationId": LOCATION_ID},
    )
    r.raise_for_status()
    return r.json()


def get_call_queue_metrics(session: requests.Session) -> dict:
    """Get call queue metrics."""
    r = session.post(
        f"{API_BASE}/phone-exp/phone-call/v1/call-queues/metrics",
        json={"locationId": LOCATION_ID},
    )
    r.raise_for_status()
    return r.json()


# --- Test Runner ---
def run_tests(session: requests.Session, do_dial: bool = False):
    print("\n" + "=" * 60)
    print("WEAVE PHONE API TEST SUITE")
    print("=" * 60)

    # Test 1: List call records
    print("\n[TEST 1] List call records (history)")
    print(f"  GET /phone-exp/phone-records/v1/call-records")
    try:
        data = list_call_records(session, page_size=5)
        records = data.get("records", [])
        print(f"  OK — {len(records)} records (limit={data.get('limit')})")
        for rec in records[:5]:
            status = rec.get("status", "?")
            direction = rec.get("direction", "?")
            caller = rec.get("callerNumber", "?")
            started = rec.get("startedAt", "?")[:19]
            print(f"    - [{direction}] {caller} → {status} at {started}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 2: Hydrated call records
    print("\n[TEST 2] Hydrated call records (with person data)")
    print(f"  GET /phone-exp/phone-records/v1/hydrated-call-records")
    try:
        data = list_hydrated_call_records(session, page_size=3)
        records = data.get("records", [])
        print(f"  OK — {len(records)} hydrated records")
        for rec in records[:3]:
            person = rec.get("person", {})
            name = f"{person.get('firstName', '?')} {person.get('lastName', '?')}" if person else "Unknown"
            print(f"    - {name}: {rec.get('direction')} {rec.get('status')} at {rec.get('startedAt', '?')[:19]}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 3: List voicemails
    print("\n[TEST 3] List voicemails")
    print(f"  GET /phone-exp/phone-records/v1/voicemails")
    try:
        data = list_voicemails(session, page_size=5)
        vms = data.get("hydratedVoicemails", [])
        print(f"  OK — {len(vms)} voicemails")
        for vm in vms[:5]:
            msg = vm.get("message", {})
            person = vm.get("person", {})
            name = f"{person.get('firstName', '?')} {person.get('lastName', '?')}" if person else "Unknown"
            print(f"    - From {name} ({msg.get('callerNumber', '?')}), "
                  f"length={msg.get('playLength', '?')}, at {msg.get('createdAt', '?')[:19]}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 4: Count unread voicemails
    print("\n[TEST 4] Count unread voicemails")
    print(f"  GET /phone-exp/phone-records/v1/count-unread-voicemails")
    try:
        data = count_unread_voicemails(session)
        counts = data.get("countPerMailbox", {})
        print(f"  OK — {json.dumps(counts)}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 5: List voicemail boxes
    print("\n[TEST 5] List voicemail boxes")
    print(f"  GET /phone-exp/phone-records/v1/voicemail-mailboxes")
    try:
        data = list_voicemail_boxes(session)
        boxes = data.get("voicemailBoxes", [])
        print(f"  OK — {len(boxes)} boxes")
        for box in boxes:
            mb = box.get("mailbox", {})
            print(f"    - {mb.get('name')} (ext {mb.get('number')}, "
                  f"id={mb.get('id', '?')[:12]}...)")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 6: Softphone settings
    print("\n[TEST 6] Softphone settings")
    print(f"  GET /phone/softphones/settings")
    try:
        data = get_softphone_settings(session)
        proxy = data.get("proxy", "?")
        softphones = data.get("softphones", [])
        print(f"  OK — proxy={proxy}")
        for sp in softphones:
            sip = sp.get("sipProfiles", [{}])[0] if sp.get("sipProfiles") else {}
            print(f"    - {sp.get('name')} (ext {sip.get('extensionNumber', '?')}, "
                  f"user={sip.get('username', '?')}@{sip.get('domain', '?')})")
            print(f"      Extensions: {len(sp.get('extensions', []))}, "
                  f"Park slots: {len(sp.get('parkSlots', []))}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 7: SIP profiles
    print("\n[TEST 7] SIP profiles")
    print(f"  GET /phone/sip-profiles/v1?tenantId={TENANT_ID[:12]}...")
    try:
        data = list_sip_profiles(session)
        profiles = data.get("sipProfiles", [])
        print(f"  OK — {len(profiles)} profiles")
        for p in profiles[:5]:
            reg = p.get("registration", {})
            device = p.get("device", {})
            active = "ACTIVE" if reg.get("active") else "offline"
            print(f"    - {p.get('name')} ({device.get('deviceType', '?')}) [{active}]"
                  f" ext={p.get('extension', {}).get('extensionNumber', '?')}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 8: Tenants
    print("\n[TEST 8] Tenants")
    print(f"  GET /phone/tenant/tenants")
    try:
        data = get_tenants(session)
        tenants = data.get("tenants", [])
        print(f"  OK — {len(tenants)} tenants")
        for t in tenants:
            locs = t.get("locations", [])
            print(f"    - {t.get('name')} (id={t.get('id', '?')[:12]}..., "
                  f"locations={len(locs)})")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 9: Call queues
    print("\n[TEST 9] Call queue metrics")
    print(f"  POST /phone-exp/phone-call/v1/call-queues/metrics")
    try:
        data = get_call_queue_metrics(session)
        print(f"  OK — keys: {list(data.keys())}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 10: Dial (only if --dial flag)
    print("\n[TEST 10] Dial Barric Reed (330-206-7819)")
    if not do_dial:
        print("  SKIP — pass --dial flag to actually place a test call")
    else:
        print(f"  POST /phone-exp/phone-call/v1/dial")
        try:
            result = dial(session, "3302067819")
            print(f"  OK — call initiated! Response: {json.dumps(result)[:100]}")
        except Exception as e:
            print(f"  FAILED: {e}")

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Test Weave Phone API client")
    parser.add_argument("--token", help="Use existing JWT token (skip browser login)")
    parser.add_argument("--dial", action="store_true",
                        help="Actually place a test call to Barric Reed (REAL CALL)")
    args = parser.parse_args()

    if args.token:
        token = args.token
        print(f"Using provided token: {token[:40]}...")
    else:
        token = login_browser()

    session = get_session(token)
    run_tests(session, do_dial=args.dial)


if __name__ == "__main__":
    main()
