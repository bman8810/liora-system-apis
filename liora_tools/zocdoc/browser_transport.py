"""Browser-backed HTTP transport for ZocDoc API calls.

DataDome blocks direct Python requests to ZocDoc endpoints. This module
provides a transport that executes fetch() calls from within a Playwright
persistent browser context, inheriting the real session cookies and
DataDome tokens.

Usage:
    transport = BrowserTransport()
    transport.start()               # launch (or reuse) the browser
    data = transport.gql(url, payload)
    data = transport.post(url, payload)
    transport.stop()                # close when done
"""

import json
import os
import time

ZOCDOC_PROFILE = os.path.expanduser("~/.zocdoc-discovery-profile")


class BrowserTransport:
    """Execute HTTP requests via Playwright browser context."""

    def __init__(self, profile_dir: str = ZOCDOC_PROFILE):
        self._profile_dir = profile_dir
        self._pw = None
        self._ctx = None
        self._page = None

    def start(self):
        """Launch or reuse the persistent browser context."""
        if self._page is not None:
            return

        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            self._profile_dir,
            headless=False,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # Navigate to ZocDoc to ensure cookies are loaded in the page context
        self._page.goto(
            "https://www.zocdoc.com/practice/pt_FMyrNSVN50CbgjEI0NcL9h/dashboard",
            wait_until="networkidle",
        )
        time.sleep(1)

    def stop(self):
        """Close the browser context."""
        if self._ctx:
            self._ctx.close()
            self._ctx = None
            self._page = None
        if self._pw:
            self._pw.stop()
            self._pw = None

    def post(self, url: str, payload: dict) -> dict:
        """Execute a POST request via browser fetch()."""
        self.start()

        result = self._page.evaluate("""
            async (args) => {
                const [url, body] = args;
                try {
                    const response = await fetch(url, {
                        method: 'POST',
                        headers: {
                            'Content-Type': 'application/json',
                            'Accept': 'application/json',
                        },
                        body: JSON.stringify(body),
                        credentials: 'include',
                    });
                    const text = await response.text();
                    return { status: response.status, body: text };
                } catch (e) {
                    return { error: e.message, status: 0 };
                }
            }
        """, [url, payload])

        if result.get("error"):
            raise RuntimeError(f"Browser fetch failed: {result['error']}")

        return result

    def gql(self, url: str, payload: dict) -> dict:
        """Execute a GraphQL POST and return parsed JSON."""
        result = self.post(url, payload)

        if result["status"] == 403:
            raise RuntimeError(
                f"ZocDoc 403 — DataDome or session expired: {result['body'][:200]}"
            )
        if result["status"] == 401:
            raise RuntimeError("ZocDoc 401 — session expired")
        if result["status"] != 200:
            raise RuntimeError(
                f"ZocDoc API error {result['status']}: {result['body'][:200]}"
            )

        return json.loads(result["body"])

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()
