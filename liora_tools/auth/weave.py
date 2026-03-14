"""Weave authentication — browser login and session building."""

import requests

from liora_tools.config import WeaveConfig


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
