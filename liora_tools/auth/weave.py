"""Weave authentication — browser login, token management, and session building."""

from __future__ import annotations

import os

import requests

from liora_tools.config import WeaveConfig

_TOKEN_ENV_VAR = "WEAVE_TOKEN"


def load_token() -> str | None:
    """Load Weave token from env var."""
    return os.environ.get(_TOKEN_ENV_VAR) or None


def ensure_session(config: WeaveConfig = None) -> tuple:
    """Try env token, then fall back to browser login.

    Returns (requests.Session, token).
    """
    config = config or WeaveConfig()

    token = load_token()
    if token:
        session = get_session(token, config)
        if _test_session(session, config):
            return session, token

    token = login_browser()
    session = get_session(token, config)
    return session, token


def _test_session(session: requests.Session, config: WeaveConfig) -> bool:
    """Quick liveness check against the threads endpoint."""
    try:
        r = session.get(
            f"{config.api_base}/sms/data/v4/threads",
            params={"locationIds": config.location_id, "pageSize": "1"},
        )
        return r.status_code == 200
    except Exception:
        return False


def login_browser() -> str:
    """Login to Weave via Playwright and return the API JWT token.

    Launches a visible browser — log in manually, then the token is
    extracted automatically from localStorage.
    """
    from playwright.sync_api import sync_playwright

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

        page.goto("https://app.getweave.com/sign-in", wait_until="networkidle")
        page.wait_for_url("**/home/**", timeout=120000)

        token = page.evaluate("localStorage.getItem('token')")
        browser.close()

        if not token:
            raise RuntimeError("No token found in localStorage after login")
        return token


def get_session(token: str, config: WeaveConfig = None) -> requests.Session:
    """Build a requests.Session with Weave auth headers."""
    config = config or WeaveConfig()
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Location-Id": config.location_id,
        "Content-Type": "application/json",
    })
    return s
