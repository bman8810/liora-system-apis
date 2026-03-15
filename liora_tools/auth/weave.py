"""Weave authentication — browser login and session building."""

import json
import os
import re
import subprocess
import time

import requests

from liora_tools.config import WeaveConfig

OUTLOOK_SCRIPT = os.path.expanduser(
    "~/.openclaw/skills/outlook/scripts/outlook-mail.sh"
)


def _fetch_mfa_code(since_ts: float, timeout: int = 90, poll: int = 5) -> str:
    """Poll Outlook inbox for a Weave Login Code email newer than *since_ts*.

    Returns the 6-digit code string.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            [OUTLOOK_SCRIPT, "inbox", "5"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            time.sleep(poll)
            continue

        # Parse the NDJSON output for a Weave Login Code email
        for line in result.stdout.strip().splitlines():
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("subject") != "Weave Login Code":
                continue

            # Read the email body to extract the code
            msg_id = msg.get("id", "")
            body_result = subprocess.run(
                [OUTLOOK_SCRIPT, "read", msg_id],
                capture_output=True, text=True,
            )
            if body_result.returncode != 0:
                continue
            try:
                body_json = json.loads(body_result.stdout)
            except json.JSONDecodeError:
                continue

            body = body_json.get("body", "")
            match = re.search(r"\b(\d{6})\b", body)
            if match:
                return match.group(1)

        time.sleep(poll)

    raise RuntimeError("Timed out waiting for Weave MFA code from Outlook")


def login_browser() -> str:
    """Login to Weave via Playwright and return the API JWT token.

    Auto-fills credentials from WEAVE_USER / WEAVE_PASSWORD env vars.
    Retrieves the MFA code from the Genie Outlook inbox automatically.
    """
    from playwright.sync_api import sync_playwright

    email = os.environ.get("WEAVE_USER", "")
    password = os.environ.get("WEAVE_PASSWORD", "")
    if not email or not password:
        raise RuntimeError(
            "WEAVE_USER and WEAVE_PASSWORD env vars are required for automated login"
        )

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

        # Navigate to sign-in
        page.goto("https://app.getweave.com/sign-in", wait_until="networkidle")

        # Fill email — use keyboard.type to trigger React validation
        email_input = page.wait_for_selector(
            'input[type="email"], input[name="email"], input[placeholder*="email" i]',
            timeout=15000,
        )
        email_input.click()
        page.keyboard.type(email, delay=50)
        time.sleep(0.5)
        page.keyboard.press("Enter")
        time.sleep(3)

        # Fill password
        pass_input = page.wait_for_selector(
            'input[type="password"]:visible', timeout=15000
        )
        pass_input.click()
        page.keyboard.type(password, delay=50)
        time.sleep(0.5)
        page.keyboard.press("Enter")
        time.sleep(2)

        # Check if we landed on home (no MFA) or need MFA
        try:
            page.wait_for_url("**/home/**", timeout=10000)
        except Exception:
            # MFA required — fetch code from Outlook
            mfa_ts = time.time()
            code = _fetch_mfa_code(mfa_ts)

            code_input = page.wait_for_selector(
                'input[type="text"], input[type="number"], input[name*="code" i], '
                'input[placeholder*="code" i], input[inputmode="numeric"]',
                timeout=15000,
            )
            code_input.fill(code)
            time.sleep(0.5)

            # Click verify/submit
            submit = page.query_selector(
                'button[type="submit"], button:has-text("Verify"), '
                'button:has-text("Continue"), button:has-text("Submit")'
            )
            if submit:
                submit.click()

            page.wait_for_url("**/home/**", timeout=30000)

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
