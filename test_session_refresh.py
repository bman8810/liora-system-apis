"""
Test session persistence and refresh strategies.
"""
import json
import os
import requests
from playwright.sync_api import sync_playwright

BASE = "https://lioraderm.ema.md"
COOKIE_FILE = "ema_cookies.json"


def save_cookies(cookies):
    with open(COOKIE_FILE, "w") as f:
        json.dump(cookies, f)
    print(f"  Saved {len(cookies)} cookies to {COOKIE_FILE}")


def load_cookies():
    if not os.path.exists(COOKIE_FILE):
        return None
    with open(COOKIE_FILE) as f:
        return json.load(f)


def make_session(cookies):
    session = requests.Session()
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c.get("path", "/"))
    return session


def test_session(session, label):
    """Quick test if a session works."""
    r = session.get(f"{BASE}/ema/ws/v3/facilities", params={"paging.pageSize": "5"}, allow_redirects=False)
    if r.status_code == 200:
        data = r.json()
        print(f"  [{label}] OK — {len(data)} facilities")
        return True
    else:
        print(f"  [{label}] FAILED — status {r.status_code}")
        if r.status_code == 302:
            print(f"    Redirected to: {r.headers.get('Location', 'unknown')}")
        return False


def login_fresh():
    """Full login flow, save cookies."""
    print("\n=== Fresh Login ===")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context()
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        page.goto(f"{BASE}/ema/Login.action", wait_until="networkidle")
        page.wait_for_selector("text=Continue as Practice Staff", timeout=15000)
        page.click("text=Continue as Practice Staff")
        page.wait_for_selector("#username", timeout=15000)

        user = os.environ.get("EMA_USER", "")
        pwd = os.environ.get("EMA_PASS", "")
        if user and pwd:
            page.click("#username")
            page.keyboard.type(user, delay=80)
            page.click("#password")
            page.keyboard.type(pwd, delay=80)
            page.keyboard.press("Enter")
        else:
            print("  Log in manually in the browser window...")

        page.wait_for_url("**/practice/staff/**", timeout=120000)
        print(f"  Logged in: {page.url.split('#')[0]}")

        cookies = context.cookies()
        browser.close()
        save_cookies(cookies)
        return cookies


def refresh_via_keycloak(cookies):
    """Try to get a new EMA session using existing Keycloak SSO cookies (no credentials needed)."""
    print("\n=== Refresh via Keycloak SSO cookies ===")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context()
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        # Pre-load the Keycloak SSO cookies so we're already "logged in" to Keycloak
        sso_cookies = [c for c in cookies if "sso.ema.md" in c.get("domain", "")]
        print(f"  Injecting {len(sso_cookies)} SSO cookies: {[c['name'] for c in sso_cookies]}")
        context.add_cookies(sso_cookies)

        # Navigate to login — if Keycloak session is valid, it should skip the form
        print("  Navigating to Login.action...")
        page.goto(f"{BASE}/ema/Login.action", wait_until="networkidle")

        # Click practice staff
        staff_btn = page.query_selector("text=Continue as Practice Staff")
        if staff_btn:
            print("  Clicking 'Continue as Practice Staff'...")
            staff_btn.click()

        # Check where we end up
        try:
            page.wait_for_url("**/practice/staff/**", timeout=15000)
            print(f"  SSO refresh worked! Redirected to: {page.url.split('#')[0]}")
            new_cookies = context.cookies()
            browser.close()
            save_cookies(new_cookies)
            return new_cookies
        except:
            current = page.url.split("?")[0]
            print(f"  SSO refresh failed — landed on: {current}")
            # Check if we're on the login form (session expired)
            if page.query_selector("#username"):
                print("  Keycloak session expired — need fresh login")
            browser.close()
            return None


if __name__ == "__main__":
    print("=" * 60)
    print("EMA Session Persistence & Refresh Test")
    print("=" * 60)

    # Step 1: Try loading saved cookies
    cookies = load_cookies()

    if cookies:
        print(f"\n=== Testing saved cookies ({len(cookies)} cookies) ===")
        session = make_session(cookies)
        if test_session(session, "saved cookies"):
            print("  Saved session still works!")

            # Step 2: Try Keycloak SSO refresh (new EMA session without re-entering credentials)
            new_cookies = refresh_via_keycloak(cookies)
            if new_cookies:
                new_session = make_session(new_cookies)
                test_session(new_session, "refreshed session")
            else:
                print("  SSO refresh not available — would need fresh login")
        else:
            print("  Saved session expired.")

            # Step 3: Try Keycloak refresh first
            new_cookies = refresh_via_keycloak(cookies)
            if new_cookies:
                new_session = make_session(new_cookies)
                test_session(new_session, "refreshed session")
            else:
                print("  Need fresh login.")
                cookies = login_fresh()
                session = make_session(cookies)
                test_session(session, "fresh login")
    else:
        print("\n  No saved cookies found. Doing fresh login...")
        cookies = login_fresh()
        session = make_session(cookies)
        test_session(session, "fresh login")
