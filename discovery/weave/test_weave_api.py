"""
Test Weave API client — authenticate via Playwright, then exercise messaging APIs.
Launches a visible browser — log in manually, then tokens are extracted automatically.

Usage:
    python3 test_weave_api.py                    # manual login, run all tests
    python3 test_weave_api.py --send             # also send a test message to Barric Reed
    python3 test_weave_api.py --token <jwt>      # skip login, use existing token

SAFETY: Send messages ONLY go to 330-206-7819 (Barric Reed) — hardcoded validation.
"""
import os
import sys
import json
import uuid
import argparse
import requests

# --- Constants ---
API_BASE = "https://api.weaveconnect.com"
LOCATION_ID = "d8508d79-c71c-4678-b139-eaedb19c2159"
LOCATION_PHONE = "+12124334569"
USER_ID = "8b835d4b-d6b3-4e81-a204-6ac39835ba2b"

# Safety: only allow sending to this number
ALLOWED_SEND_PHONE = "+13302067819"  # Barric Reed


def get_session(token: str) -> requests.Session:
    """Create a requests.Session with Weave auth headers."""
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Location-Id": LOCATION_ID,
        "Content-Type": "application/json",
    })
    return s


# --- Auth ---
def login_browser() -> str:
    """Login to Weave via Playwright and return the API JWT token."""
    from playwright.sync_api import sync_playwright

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


# --- Messaging APIs ---
def list_threads(session: requests.Session, page_size: int = 10) -> dict:
    """List inbox threads."""
    r = session.get(
        f"{API_BASE}/sms/data/v4/threads",
        params={"locationIds": LOCATION_ID, "pageSize": str(page_size)},
    )
    r.raise_for_status()
    return r.json()


def get_thread(session: requests.Session, thread_id: str, page_size: int = 25) -> dict:
    """Get messages in a specific thread."""
    r = session.get(
        f"{API_BASE}/sms/data/v4/unified/threads/{thread_id}",
        params={"locationId": LOCATION_ID, "pageSize": str(page_size)},
    )
    r.raise_for_status()
    return r.json()


def send_message(session: requests.Session, person_phone: str, body: str,
                 person_id: str = None) -> dict:
    """
    Send an SMS message.
    SAFETY: Will only send to ALLOWED_SEND_PHONE.
    """
    # Normalize phone
    phone = person_phone.replace("-", "").replace("(", "").replace(")", "").replace(" ", "")
    if not phone.startswith("+"):
        phone = "+1" + phone

    if phone != ALLOWED_SEND_PHONE:
        raise ValueError(
            f"SAFETY: Refusing to send to {phone}. "
            f"Only {ALLOWED_SEND_PHONE} (Barric Reed) is allowed."
        )

    payload = {
        "locationId": LOCATION_ID,
        "locationPhone": LOCATION_PHONE,
        "personPhone": phone,
        "programSlugId": "manual-messages",
        "createdBy": USER_ID,
        "shortenUrls": True,
        "messageType": "MESSAGING_MANUAL",
        "body": body,
        "media": [],
        "relatedIds": [],
        "id": str(uuid.uuid4()),
    }
    if person_id:
        payload["personId"] = person_id

    r = session.post(f"{API_BASE}/sms/send/v3", json=payload)
    r.raise_for_status()
    return r.json() if r.text else {"status": r.status_code}


# --- Person/Contact APIs ---
def search_persons(session: requests.Session, query: str, page_size: int = 25) -> dict:
    """Search for persons/contacts by name or phone."""
    r = session.post(
        f"{API_BASE}/persons/v3/persons/search",
        json={
            "query": query,
            "locationIds": [LOCATION_ID],
            "pageSize": page_size,
        },
    )
    r.raise_for_status()
    return r.json()


def lookup_by_phone(session: requests.Session, phone: str) -> dict:
    """Look up a person by phone number."""
    # Ensure E.164 format
    phone = phone.replace("-", "").replace("(", "").replace(")", "").replace(" ", "")
    if not phone.startswith("+"):
        phone = "+1" + phone

    r = session.get(
        f"{API_BASE}/persons/v3/locations/{LOCATION_ID}/primary-contact",
        params={"phoneNumber": phone},
    )
    r.raise_for_status()
    return r.json()


def get_person(session: requests.Session, person_id: str) -> dict:
    """Get full person details by ID."""
    r = session.get(f"{API_BASE}/persons/v3/persons/{person_id}")
    r.raise_for_status()
    return r.json()


# --- Test Runner ---
def run_tests(session: requests.Session, do_send: bool = False):
    print("\n" + "=" * 60)
    print("WEAVE API TEST SUITE")
    print("=" * 60)

    # Test 1: List threads
    print("\n[TEST 1] List inbox threads")
    print(f"  GET /sms/data/v4/threads?locationIds={LOCATION_ID}&pageSize=5")
    try:
        data = list_threads(session, page_size=5)
        threads = data.get("threads", [])
        print(f"  OK — {len(threads)} threads returned")
        for t in threads[:5]:
            person = t.get("person", {})
            name = f"{person.get('firstName', '?')} {person.get('lastName', '?')}"
            msgs = t.get("messages", [])
            last_msg = msgs[0].get("body", "")[:50] if msgs else "(no messages)"
            print(f"    - {name}: {last_msg}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 2: Get thread detail
    print("\n[TEST 2] Get thread detail")
    try:
        threads = list_threads(session, page_size=1).get("threads", [])
        if threads:
            tid = threads[0]["id"]
            print(f"  GET /sms/data/v4/unified/threads/{tid[:12]}...")
            detail = get_thread(session, tid, page_size=3)
            items = detail.get("thread", {}).get("items", [])
            print(f"  OK — {len(items)} messages in thread")
            for item in items[:3]:
                msg = item.get("smsMessage", {})
                direction = "OUT" if "OUTBOUND" in msg.get("direction", "") else "IN"
                print(f"    [{direction}] {msg.get('body', '')[:60]}")
        else:
            print("  SKIP — no threads available")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 3: Search persons
    print("\n[TEST 3] Search persons")
    print("  POST /persons/v3/persons/search  body: {query: 'Barric'}")
    try:
        data = search_persons(session, "Barric")
        persons = data.get("persons", [])
        print(f"  OK — {len(persons)} results")
        for p in persons[:5]:
            print(f"    - {p.get('firstName')} {p.get('lastName')} "
                  f"(id={p.get('personId', '?')[:12]}..., status={p.get('status')})")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 4: Phone lookup
    print("\n[TEST 4] Lookup person by phone")
    print(f"  GET /persons/v3/locations/.../primary-contact?phoneNumber=+13302067819")
    try:
        data = lookup_by_phone(session, "3302067819")
        print(f"  OK — personId={data.get('personId', '?')[:12]}..., "
              f"phone={data.get('phoneNumber')}")
    except Exception as e:
        print(f"  FAILED: {e}")

    # Test 5: Send message (only if --send flag)
    print("\n[TEST 5] Send message to Barric Reed (330-206-7819)")
    if not do_send:
        print("  SKIP — pass --send flag to actually send a test message")
    else:
        print(f"  POST /sms/send/v3")
        try:
            # Look up Barric's person ID first
            contact = lookup_by_phone(session, "3302067819")
            person_id = contact.get("personId")

            result = send_message(
                session,
                person_phone="3302067819",
                body="Weave API test from Python client - please ignore",
                person_id=person_id,
            )
            print(f"  OK — message sent! Response: {json.dumps(result)[:100]}")
        except Exception as e:
            print(f"  FAILED: {e}")

    print("\n" + "=" * 60)
    print("DONE")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Test Weave API client")
    parser.add_argument("--token", help="Use existing JWT token (skip browser login)")
    parser.add_argument("--send", action="store_true",
                        help="Actually send a test message to Barric Reed")
    args = parser.parse_args()

    if args.token:
        token = args.token
        print(f"Using provided token: {token[:40]}...")
    else:
        token = login_browser()

    session = get_session(token)
    run_tests(session, do_send=args.send)


if __name__ == "__main__":
    main()
