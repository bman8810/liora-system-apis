"""Zocdoc authentication — browser login, cookie management, DataDome bypass."""

import json
import os
import time

import requests

from liora_tools.config import ZocdocConfig


def login_browser() -> list:
    """Login to Zocdoc via Playwright and return cookies.

    Uses a persistent Chrome profile. Reads credentials from
    ZOCDOC_EMAIL / ZOCDOC_PASSWORD env vars. Fails with a clear error
    if not set and login form is encountered.
    """
    from playwright.sync_api import sync_playwright

    email = os.environ.get("ZOCDOC_EMAIL", "")
    password = os.environ.get("ZOCDOC_PASSWORD", "")

    profile = os.path.expanduser("~/.zocdoc-discovery-profile")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile, channel="chrome", headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page.goto("https://www.zocdoc.com/signin?provider=1", wait_until="networkidle")
        time.sleep(2)

        if "/practice/" not in page.url and "/provider/" not in page.url:
            if not email or not password:
                ctx.close()
                raise RuntimeError(
                    "Zocdoc login required but ZOCDOC_EMAIL / ZOCDOC_PASSWORD "
                    "env vars are not set. Set them or provide saved cookies."
                )

            email_input = page.wait_for_selector(
                'input[type="email"], input[name="email"]', timeout=10000
            )
            email_input.fill(email)
            time.sleep(0.5)
            page.click('button[type="submit"]')
            time.sleep(3)

            pass_input = page.wait_for_selector(
                'input[type="password"]:visible', timeout=10000
            )
            pass_input.fill(password)
            time.sleep(0.5)
            page.click('button[type="submit"]')
            page.wait_for_url("**/practice/**", timeout=30000)

        cookies = ctx.cookies()
        zocdoc_cookies = [c for c in cookies if "zocdoc" in c.get("domain", "")]
        ctx.close()
        return zocdoc_cookies


def load_cookies(path: str = None) -> list | None:
    path = path or ZocdocConfig().cookie_file
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def save_cookies(cookies: list, path: str = None) -> None:
    path = path or ZocdocConfig().cookie_file
    with open(path, "w") as f:
        json.dump(cookies, f, indent=2)


def get_session(cookies: list, config: ZocdocConfig = None) -> requests.Session:
    """Build a requests.Session with Zocdoc auth cookies and headers."""
    config = config or ZocdocConfig()
    s = requests.Session()

    cookie_dict = {c["name"]: c["value"] for c in cookies}
    datadome_id = cookie_dict.get("datadome", "")

    s.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.zocdoc.com",
        "Referer": f"https://www.zocdoc.com/provider/inbox/{config.practice_id}",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "x-datadome-clientid": datadome_id,
    })

    for c in cookies:
        domain = c.get("domain", ".zocdoc.com")
        if not domain.startswith(".") and not domain.startswith("www"):
            domain = "." + domain
        s.cookies.set(
            c["name"], c["value"],
            domain=domain, path=c.get("path", "/"),
        )

    return s


def send_call_request_browser(request_id: str, reasons: list = None,
                               config: ZocdocConfig = None) -> dict:
    """Send 'call the office' request via browser fetch (DataDome bypass).

    Uses Playwright to make the REST call from within the browser context.
    Reads credentials from ZOCDOC_EMAIL / ZOCDOC_PASSWORD env vars if login is needed.
    """
    from playwright.sync_api import sync_playwright

    config = config or ZocdocConfig()
    reasons = reasons or ["Other"]

    email = os.environ.get("ZOCDOC_EMAIL", "")
    password = os.environ.get("ZOCDOC_PASSWORD", "")
    profile = os.path.expanduser("~/.zocdoc-discovery-profile")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            profile, channel="chrome", headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        page.goto(
            f"https://www.zocdoc.com/provider/inbox/{config.practice_id}",
            wait_until="networkidle",
        )
        time.sleep(2)

        if "signin" in page.url:
            if not email or not password:
                ctx.close()
                raise RuntimeError(
                    "Zocdoc login required but ZOCDOC_EMAIL / ZOCDOC_PASSWORD "
                    "env vars are not set."
                )
            email_input = page.wait_for_selector(
                'input[type="email"], input[name="email"]', timeout=10000
            )
            email_input.fill(email)
            time.sleep(0.5)
            page.click('button[type="submit"]')
            time.sleep(3)
            pass_input = page.wait_for_selector(
                'input[type="password"]:visible', timeout=10000
            )
            pass_input.fill(password)
            time.sleep(0.5)
            page.click('button[type="submit"]')
            page.wait_for_url("**/practice/**", timeout=30000)
            page.goto(
                f"https://www.zocdoc.com/provider/inbox/{config.practice_id}",
                wait_until="networkidle",
            )
            time.sleep(2)

        result = page.evaluate("""
            async (args) => {
                const [requestId, reasons] = args;
                try {
                    const response = await fetch(
                        '/provider/api/appointments/RequestPatientCall',
                        {
                            method: 'POST',
                            headers: {
                                'Content-Type': 'application/json',
                                'Accept': 'application/json',
                            },
                            body: JSON.stringify({
                                apptId: requestId,
                                requestedInformation: reasons
                            }),
                            credentials: 'include'
                        }
                    );
                    const text = await response.text();
                    return { status: response.status, body: text };
                } catch (e) {
                    return { error: e.message };
                }
            }
        """, [str(request_id), reasons])

        cookies = ctx.cookies()
        zocdoc_cookies = [c for c in cookies if "zocdoc" in c.get("domain", "")]
        save_cookies(zocdoc_cookies, config.cookie_file)

        ctx.close()

    if result.get("error"):
        raise RuntimeError(f"Browser fetch failed: {result['error']}")
    if result["status"] != 200:
        raise RuntimeError(
            f"RequestPatientCall returned {result['status']}: {result['body']}"
        )
    return result
