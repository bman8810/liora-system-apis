"""
Test EMA authentication via Playwright and verify API access.
Launches a visible browser — log in manually, then cookies are extracted automatically.
Usage: EMA_USER=xxx EMA_PASS=xxx python3 test_ema_auth.py
       or just: python3 test_ema_auth.py  (manual login mode)
"""
import os
import sys
import json
import requests
from playwright.sync_api import sync_playwright

BASE = "https://lioraderm.ema.md"

EMA_USER = os.environ.get("EMA_USER")
EMA_PASS = os.environ.get("EMA_PASS")


def login():
    print("[1] Launching visible browser...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context()
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        print("[2] Navigating to login page...")
        page.goto(f"{BASE}/ema/Login.action", wait_until="networkidle")

        print("[3] Clicking 'Continue as Practice Staff'...")
        page.wait_for_selector("text=Continue as Practice Staff", timeout=15000)
        page.click("text=Continue as Practice Staff")
        page.wait_for_selector("#username", timeout=15000)

        if EMA_USER and EMA_PASS:
            print("[4] Typing credentials with keyboard simulation...")
            # Click into the field and type character by character
            page.click("#username")
            page.keyboard.type(EMA_USER, delay=80)
            page.click("#password")
            page.keyboard.type(EMA_PASS, delay=80)
            page.keyboard.press("Enter")
        else:
            print("[4] Please log in manually in the browser window...")

        print("    Waiting for redirect to EMA dashboard...")
        page.wait_for_url("**/practice/staff/**", timeout=120000)
        print(f"    Login successful! URL: {page.url.split('#')[0]}")

        print("[5] Extracting cookies...")
        cookies = context.cookies()
        browser.close()

        cookie_names = [c["name"] for c in cookies]
        print(f"    Got {len(cookies)} cookies: {cookie_names}")

        return cookies


def test_api(cookies):
    session = requests.Session()
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))

    print("\n[6] Testing API endpoints...\n")

    # Test 1: Facilities
    print("  GET /ema/ws/v3/facilities")
    r = session.get(f"{BASE}/ema/ws/v3/facilities", params={"paging.pageSize": "5"})
    print(f"    Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"    Facilities: {len(data)} found")
        for f in data:
            print(f"      - {f.get('name')} (id={f.get('id')})")

    # Test 2: Patients (first 3)
    print("\n  GET /ema/ws/v3/patients")
    r = session.get(f"{BASE}/ema/ws/v3/patients", params={
        "selector": "lastName,firstName,mrn,id",
        "where": 'fn=patientStatus="\\"ACTIVE\\""',
        "paging.pageSize": "3",
        "paging.pageNumber": "1",
        "sorting.sortBy": "lastName",
        "sorting.sortOrder": "ASC",
    })
    print(f"    Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"    Patients: {len(data)} returned")

    # Test 3: Today's appointments
    print("\n  GET /ema/ws/v3/appointments (today)")
    from datetime import date
    today = date.today().isoformat()
    r = session.get(f"{BASE}/ema/ws/v3/appointments", params={
        "selector": "id,scheduledStartDateLd,appointmentTypeName,status",
        "where": f'scheduledStartDateLd=="{today}"',
        "paging.pageSize": "50",
    })
    print(f"    Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"    Appointments today: {len(data)}")

    # Test 4: Appointment types
    print("\n  GET /ema/ws/v3/appointmentType")
    r = session.get(f"{BASE}/ema/ws/v3/appointmentType", params={
        "paging.pageSize": "100",
        "selector": "id,name,defaultDuration",
    })
    print(f"    Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        print(f"    Appointment types: {len(data)}")

    # Test 5: Appointment finder (available slots)
    print("\n  GET /ema/ws/v2/appointment/finder")
    r = session.get(f"{BASE}/ema/ws/v2/appointment/finder", params={
        "apptTypeId": "6188",
        "duration": "15",
        "timeOfDay": "ANYTIME",
        "timeFrame": "FIRST_AVAILABLE",
        "display": "BY_PROVIDER",
        "specificDate": f"{today}T00:00:00.000Z",
        "alternateDurationsIncluded": "true",
        "canOverrideDefaults": "true",
        "afterFirstAppointment": "false",
        "timeBuffer": "0",
    })
    print(f"    Status: {r.status_code}")
    if r.status_code == 200:
        data = r.json()
        total_slots = sum(len(p.get("appointments", [])) for p in data)
        print(f"    Providers with availability: {len(data)}")
        print(f"    Total available slots: {total_slots}")

    print("\n[7] All tests complete!")


if __name__ == "__main__":
    cookies = login()
    test_api(cookies)
