"""Extract auth credentials from Chrome browser and save to shared credential files.

This module supports a workflow where Claude for Chrome (running on Windows)
handles the browser login (bot detection, MFA, etc.) and then extracts
tokens/cookies via JavaScript. The extracted data is saved to the shared
credential directory so WSL2/Ubuntu-Genie can use it.

Workflow:
    1. Claude for Chrome navigates to login page and authenticates
    2. Claude executes JS snippets (provided below) to extract auth data
    3. Claude calls save functions (or CLI) to persist credentials
    4. WSL2 reads credentials from /mnt/c/Users/barri/.liora/credentials/

Usage (CLI):
    # Weave — pipe JWT token
    echo '{"token":"eyJ..."}' | python -m liora_tools auth save-chrome weave

    # EMA — pipe cookies as JSON array
    echo '[{"name":"JSESSIONID","value":"abc","domain":"lioraderm.ema.md"}]' \\
        | python -m liora_tools auth save-chrome ema

    # Zocdoc — pipe cookies as JSON array
    echo '[{"name":"datadome","value":"xyz","domain":".zocdoc.com"}]' \\
        | python -m liora_tools auth save-chrome zocdoc
"""

import json
import sys
from datetime import datetime, timezone

from liora_tools.auth.session_manager import save_credentials

# ── JavaScript Extraction Snippets ──────────────────────────────────────────
# These are meant to be executed via mcp__claude-in-chrome__javascript_tool
# after a successful login in each platform's Chrome tab.

WEAVE_EXTRACT_JS = """\
// Run on app.getweave.com after login
localStorage.getItem('token')
"""

EMA_EXTRACT_JS = """\
// Run on lioraderm.ema.md after login
// Returns all accessible cookies as a structured array
document.cookie.split(';').map(c => {
    const [name, ...rest] = c.trim().split('=');
    return {name: name, value: rest.join('='), domain: location.hostname, path: '/'};
})
"""

ZOCDOC_EXTRACT_JS = """\
// Run on zocdoc.com after login
// Returns all accessible cookies as a structured array
document.cookie.split(';').map(c => {
    const [name, ...rest] = c.trim().split('=');
    return {name: name, value: rest.join('='), domain: location.hostname, path: '/'};
})
"""

# For EMA, we also need SSO cookies from the Keycloak domain.
# After EMA login, navigate to sso.ema.md to extract those too.
EMA_SSO_EXTRACT_JS = """\
// Run on sso.ema.md (navigate there briefly after EMA login)
document.cookie.split(';').map(c => {
    const [name, ...rest] = c.trim().split('=');
    return {name: name, value: rest.join('='), domain: location.hostname, path: '/'};
})
"""

# ── Network-based cookie extraction ─────────────────────────────────────────
# If document.cookie misses httpOnly cookies, use this approach:
# 1. After login, use mcp__claude-in-chrome__read_network_requests to find
#    requests to the target domain
# 2. Look for Cookie/Set-Cookie headers in the captured data
# 3. Parse and save those cookies

EMA_FETCH_TEST_JS = """\
// Make a test API call — cookies are sent automatically
// Check mcp__claude-in-chrome__read_network_requests after this
fetch('/ema/ws/v3/facilities?paging.pageSize=1', {credentials: 'include'})
    .then(r => r.json())
    .then(d => JSON.stringify(d))
"""


# ── Save Functions ──────────────────────────────────────────────────────────


def save_weave_from_chrome(token: str) -> dict:
    """Save a Weave JWT token extracted from Chrome localStorage."""
    if not token or not token.startswith("eyJ"):
        raise ValueError("Invalid Weave token — expected a JWT starting with 'eyJ'")
    now_iso = datetime.now(timezone.utc).isoformat()
    save_credentials("weave", {"token": token, "refreshed_at": now_iso})
    return {"status": "saved", "platform": "weave", "refreshed_at": now_iso}


def save_ema_from_chrome(cookies: list) -> dict:
    """Save EMA cookies extracted from Chrome.

    Args:
        cookies: List of cookie dicts with at least 'name' and 'value' keys.
                 Should include cookies from both lioraderm.ema.md and sso.ema.md.
    """
    if not cookies:
        raise ValueError("No cookies provided")

    # Normalize: ensure each cookie has required fields
    normalized = []
    for c in cookies:
        normalized.append({
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", "lioraderm.ema.md"),
            "path": c.get("path", "/"),
        })

    now_iso = datetime.now(timezone.utc).isoformat()
    save_credentials("ema", {"cookies": normalized, "last_verified": now_iso})
    return {
        "status": "saved", "platform": "ema",
        "cookie_count": len(normalized), "refreshed_at": now_iso,
    }


def save_zocdoc_from_chrome(cookies: list) -> dict:
    """Save Zocdoc cookies extracted from Chrome.

    Args:
        cookies: List of cookie dicts with at least 'name' and 'value' keys.
                 Must include the 'datadome' cookie for DataDome bypass.
    """
    if not cookies:
        raise ValueError("No cookies provided")

    normalized = []
    has_datadome = False
    for c in cookies:
        name = c["name"]
        if name == "datadome":
            has_datadome = True
        normalized.append({
            "name": name,
            "value": c["value"],
            "domain": c.get("domain", ".zocdoc.com"),
            "path": c.get("path", "/"),
        })

    now_iso = datetime.now(timezone.utc).isoformat()
    save_credentials("zocdoc", {"cookies": normalized, "last_verified": now_iso})
    result = {
        "status": "saved", "platform": "zocdoc",
        "cookie_count": len(normalized), "refreshed_at": now_iso,
    }
    if not has_datadome:
        result["warning"] = "No 'datadome' cookie found — API calls may be blocked"
    return result


def save_from_chrome(platform: str, data: str) -> dict:
    """Save Chrome-extracted auth data for a platform.

    Args:
        platform: One of 'weave', 'ema', 'zocdoc'.
        data: JSON string — token string for Weave, cookie array for EMA/Zocdoc.
    """
    parsed = json.loads(data)

    if platform == "weave":
        if isinstance(parsed, dict):
            token = parsed.get("token", "")
        else:
            token = str(parsed)
        return save_weave_from_chrome(token)

    elif platform == "ema":
        if isinstance(parsed, dict):
            cookies = parsed.get("cookies", [])
        else:
            cookies = parsed
        return save_ema_from_chrome(cookies)

    elif platform == "zocdoc":
        if isinstance(parsed, dict):
            cookies = parsed.get("cookies", [])
        else:
            cookies = parsed
        return save_zocdoc_from_chrome(cookies)

    else:
        raise ValueError(f"Unknown platform: {platform}. Expected: weave, ema, zocdoc")


# ── CLI Entrypoint ──────────────────────────────────────────────────────────


def main():
    """CLI: python -m liora_tools auth save-chrome <platform>

    Reads JSON from stdin. Outputs result as JSON to stdout.
    """
    if len(sys.argv) < 2:
        print("Usage: ... auth save-chrome <weave|ema|zocdoc>", file=sys.stderr)
        print("Reads JSON from stdin.", file=sys.stderr)
        sys.exit(1)

    platform = sys.argv[1]
    data = sys.stdin.read().strip()

    try:
        result = save_from_chrome(platform, data)
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    except Exception as e:
        json.dump({"error": str(e)}, sys.stderr, indent=2)
        sys.stderr.write("\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
