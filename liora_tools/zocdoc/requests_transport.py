"""Cookie-based HTTP transport for ZocDoc API calls.

Alternative to BrowserTransport for environments without Playwright (e.g. WSL2).
Uses requests.Session with saved cookies + DataDome header. This may not work
if DataDome actively blocks non-browser requests, but works when the datadome
cookie is fresh from a real Chrome session.

Usage:
    from liora_tools.zocdoc.requests_transport import RequestsTransport
    transport = RequestsTransport(cookies)
    client = ZocdocClient(transport, config)
"""

import json

import requests

from liora_tools.config import ZocdocConfig


class RequestsTransport:
    """Execute HTTP requests via requests.Session with saved cookies."""

    def __init__(self, cookies: list, config: ZocdocConfig = None):
        self._config = config or ZocdocConfig()
        self._session = _build_session(cookies, self._config)

    def start(self):
        """No-op — session is ready immediately."""
        pass

    def stop(self):
        """Close the requests session."""
        self._session.close()

    def post(self, url: str, payload: dict) -> dict:
        """Execute a POST request via requests.Session."""
        try:
            r = self._session.post(url, json=payload, timeout=30)
            return {"status": r.status_code, "body": r.text}
        except requests.RequestException as e:
            return {"error": str(e), "status": 0}

    def gql(self, url: str, payload: dict) -> dict:
        """Execute a GraphQL POST and return parsed JSON."""
        result = self.post(url, payload)

        if result.get("error"):
            raise RuntimeError(f"Request failed: {result['error']}")
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


def _build_session(cookies: list, config: ZocdocConfig) -> requests.Session:
    """Build a requests.Session with Zocdoc auth cookies and headers."""
    s = requests.Session()

    cookie_dict = {c["name"]: c["value"] for c in cookies}
    datadome_id = cookie_dict.get("datadome", "")

    s.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": "https://www.zocdoc.com",
        "Referer": f"https://www.zocdoc.com/provider/inbox/{config.practice_id}",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
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
