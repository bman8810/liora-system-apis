"""EMA (ModMed) authentication — browser login, Keycloak SSO refresh, cookie management."""

from __future__ import annotations

import json
import os

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
