"""EMA (ModMed) authentication — browser login, Keycloak SSO refresh, cookie management."""

from __future__ import annotations

import json
import os
import re

import requests

from liora_tools.config import EmaConfig


def login_browser(username: str = None, password: str = None) -> list:
    """Login to EMA via Playwright and return cookies.

    If username/password are not provided, reads EMA_USER / EMA_PASS from env.
    Falls back to manual login if neither is available.
    """
    from playwright.sync_api import sync_playwright

    username = username or os.environ.get("EMA_USER", "")
    password = password or os.environ.get("EMA_PASS", "")
    config = EmaConfig()

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

        page.goto(f"{config.base_url}/ema/Login.action", wait_until="networkidle")
        page.wait_for_selector("text=Continue as Practice Staff", timeout=15000)
        page.click("text=Continue as Practice Staff")
        page.wait_for_selector("#username", timeout=15000)

        if username and password:
            page.click("#username")
            page.keyboard.type(username, delay=80)
            page.click("#password")
            page.keyboard.type(password, delay=80)
            page.keyboard.press("Enter")

        page.wait_for_url("**/practice/staff/**", timeout=120000)
        cookies = context.cookies()
        browser.close()
        return cookies


def refresh_via_keycloak(cookies: list) -> list | None:
    """Try to get a new EMA session using existing Keycloak SSO cookies.

    Returns refreshed cookies or None if SSO session is expired.
    """
    from playwright.sync_api import sync_playwright

    config = EmaConfig()

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

        sso_cookies = [c for c in cookies if "sso.ema.md" in c.get("domain", "")]
        context.add_cookies(sso_cookies)

        page.goto(f"{config.base_url}/ema/Login.action", wait_until="networkidle")

        staff_btn = page.query_selector("text=Continue as Practice Staff")
        if staff_btn:
            staff_btn.click()

        try:
            page.wait_for_url("**/practice/staff/**", timeout=15000)
            new_cookies = context.cookies()
            browser.close()
            return new_cookies
        except Exception:
            browser.close()
            return None


def refresh_via_sso_http(sso_cookies: list, config: EmaConfig = None) -> list | None:
    """Get a new EMA session using Keycloak SSO cookies via plain HTTP — no Playwright.

    Works from WSL2 or any environment without a browser. The flow:
      1. GET /ema/Login.action  →  EMA redirects to Keycloak with a fresh code_challenge
      2. Keycloak sees KEYCLOAK_SESSION cookie  →  silent re-auth  →  redirects back to EMA
      3. EMA callback exchanges code for session  →  Set-Cookie headers (incl. httpOnly)
      4. requests.Session captures all Set-Cookie headers and returns full cookie list

    Args:
        sso_cookies: List of cookie dicts from sso.ema.md (must include KEYCLOAK_SESSION).

    Returns:
        List of session cookies (all domains) or None if SSO session is expired.
    """
    config = config or EmaConfig()

    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })

    # Inject SSO cookies onto sso.ema.md domain
    for c in sso_cookies:
        domain = c.get("domain", "")
        if "sso.ema.md" in domain:
            session.cookies.set(c["name"], c["value"], domain="sso.ema.md", path="/")

    try:
        # Step 1: GET EMA login page — EMA generates PKCE and redirects to Keycloak
        r1 = session.get(
            f"{config.base_url}/ema/Login.action",
            allow_redirects=True, timeout=30,
        )

        # If already on dashboard (no login needed), extract cookies and return
        if "practice/staff" in r1.url:
            return _extract_cookies(session)

        # Step 2: Parse the "Continue as Practice Staff" form.
        # Login.action renders a POST form (not an href) with hidden fields:
        #   __disable__, _sourcePage, __fp
        # The submit button name is "redirectToNonPatientLoginPage".
        hidden_fields = {}
        for input_tag in re.finditer(r'<input[^>]+>', r1.text):
            tag = input_tag.group(0)
            if 'hidden' not in tag:
                continue
            name_m = re.search(r'name=["\']([^"\']+)["\']', tag)
            value_m = re.search(r'value=["\']([^"\']*)["\']', tag)
            if name_m and value_m:
                hidden_fields[name_m.group(1)] = value_m.group(1)

        if not hidden_fields:
            return None  # Page structure unexpected

        # Build form data: hidden fields + the submit button
        form_data = {**hidden_fields, "redirectToNonPatientLoginPage": ""}

        # Step 3: POST Login.action — EMA redirects to Keycloak, which sees
        # KEYCLOAK_SESSION and silently re-auths, then redirects back to EMA,
        # setting fresh session cookies via Set-Cookie headers.
        r2 = session.post(
            f"{config.base_url}/ema/Login.action",
            data=form_data,
            allow_redirects=True,
            timeout=30,
        )

        if "practice/staff" in r2.url:
            return _extract_cookies(session)

        return None

    except Exception:
        return None


def _extract_cookies(session: requests.Session) -> list:
    """Extract all cookies from a requests.Session as a list of dicts."""
    cookies = []
    for c in session.cookies:
        cookies.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain or "lioraderm.ema.md",
            "path": c.path or "/",
        })
    return cookies or None


def load_cookies(path: str = None) -> list | None:
    """Load cookies from a JSON file."""
    path = path or EmaConfig().cookie_file
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def save_cookies(cookies: list, path: str = None) -> None:
    """Save cookies to a JSON file."""
    path = path or EmaConfig().cookie_file
    with open(path, "w") as f:
        json.dump(cookies, f, indent=2)


def ensure_session(cookies: list = None, config: EmaConfig = None) -> tuple:
    """Three-tier session strategy: reuse -> SSO refresh -> fresh login.

    Returns (requests.Session, cookies).
    """
    config = config or EmaConfig()

    if cookies is None:
        cookies = load_cookies(config.cookie_file)

    if cookies:
        session = _make_session(cookies, config)
        if _test_session(session, config):
            return session, cookies

        new_cookies = refresh_via_keycloak(cookies)
        if new_cookies:
            save_cookies(new_cookies, config.cookie_file)
            session = _make_session(new_cookies, config)
            if _test_session(session, config):
                return session, new_cookies

    fresh = login_browser()
    save_cookies(fresh, config.cookie_file)
    session = _make_session(fresh, config)
    return session, fresh


def _make_session(cookies: list, config: EmaConfig) -> requests.Session:
    session = requests.Session()
    for c in cookies:
        session.cookies.set(
            c["name"], c["value"],
            domain=c["domain"], path=c.get("path", "/"),
        )
    return session


def _test_session(session: requests.Session, config: EmaConfig) -> bool:
    try:
        r = session.get(
            f"{config.base_url}/ema/ws/v3/facilities",
            params={"paging.pageSize": "1"},
            allow_redirects=False,
        )
        return r.status_code == 200
    except Exception:
        return False
