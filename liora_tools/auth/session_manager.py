"""Credential consolidation and session management for liora_tools platforms.

Credentials are stored in ~/.openclaw/credentials/liora/ (configurable via
LIORA_CREDENTIALS_DIR env var). Each platform has its own JSON file:

  weave_token.json  — JWT token + refreshed_at timestamp
  ema_cookies.json  — cookie list + last_verified timestamp
  zocdoc_cookies.json — cookie list + last_verified timestamp

The get_client() factory validates the session on each call and attempts
auto-refresh before raising AuthenticationError.
"""

import json
from datetime import datetime, timezone

from liora_tools.config import CREDENTIALS_DIR, CREDENTIAL_FILES
from liora_tools.exceptions import AuthenticationError


def _ensure_dir():
    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)


def _cred_path(platform: str):
    if platform not in CREDENTIAL_FILES:
        raise ValueError(f"Unknown platform: {platform}. Expected: {list(CREDENTIAL_FILES)}")
    return CREDENTIALS_DIR / CREDENTIAL_FILES[platform]


def load_credentials(platform: str) -> dict | None:
    """Load credentials for a platform. Returns None if file not found."""
    path = _cred_path(platform)
    if not path.exists():
        return None
    with open(path) as f:
        return json.load(f)


def save_credentials(platform: str, data: dict) -> None:
    """Save credentials for a platform."""
    _ensure_dir()
    path = _cred_path(platform)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ── Platform-specific client factories ──


def get_weave_client():
    """Get a validated WeaveClient. Raises AuthenticationError if token is missing/expired."""
    from liora_tools.auth.weave import get_session
    from liora_tools.config import WeaveConfig
    from liora_tools.weave.client import WeaveClient

    config = WeaveConfig()
    creds = load_credentials("weave")
    if not creds or "token" not in creds:
        raise AuthenticationError(
            "No Weave token found. Run: python -m liora_tools auth refresh weave"
        )

    session = get_session(creds["token"], config)
    client = WeaveClient(session, config)

    # Validate with a lightweight call
    try:
        client.list_threads(page_size=1)
    except AuthenticationError:
        raise AuthenticationError(
            "Weave token expired. Run: python -m liora_tools auth refresh weave"
        )
    return client


def get_ema_client():
    """Get a validated EmaClient with SSO auto-refresh."""
    from liora_tools.auth import ema as ema_auth
    from liora_tools.config import EmaConfig
    from liora_tools.modmed.client import EmaClient

    config = EmaConfig()
    creds = load_credentials("ema")

    # Support both new format {"cookies": [...]} and legacy flat array
    cookies = None
    if creds:
        cookies = creds.get("cookies") if isinstance(creds, dict) else creds

    if cookies:
        client = EmaClient.from_cookies(cookies, config)
        if client.check_session():
            return client

        # Try Keycloak SSO refresh (headless-ish, ~3s)
        try:
            new_cookies = ema_auth.refresh_via_keycloak(cookies)
            if new_cookies:
                save_credentials("ema", {
                    "cookies": new_cookies,
                    "last_verified": datetime.now(timezone.utc).isoformat(),
                })
                client = EmaClient.from_cookies(new_cookies, config)
                if client.check_session():
                    return client
        except Exception:
            pass

    raise AuthenticationError(
        "EMA session expired. Run: python -m liora_tools auth refresh ema"
    )


def get_zocdoc_client():
    """Get a validated ZocdocClient with session refresh."""
    from liora_tools.auth.zocdoc import get_session
    from liora_tools.config import ZocdocConfig
    from liora_tools.zocdoc.client import ZocdocClient

    config = ZocdocConfig()
    creds = load_credentials("zocdoc")

    cookies = None
    if creds:
        cookies = creds.get("cookies") if isinstance(creds, dict) else creds

    if not cookies:
        raise AuthenticationError(
            "No ZocDoc cookies found. Run: python -m liora_tools auth refresh zocdoc"
        )

    session = get_session(cookies, config)
    client = ZocdocClient(session, config)

    # Validate with a lightweight call
    try:
        client.get_status_counts()
        return client
    except AuthenticationError:
        pass

    # Try inline session refresh
    try:
        client.refresh_session()
        client.get_status_counts()
        return client
    except Exception:
        raise AuthenticationError(
            "ZocDoc session expired. Run: python -m liora_tools auth refresh zocdoc"
        )


# ── Public API ──


def get_client(platform: str):
    """Factory: get a validated client for the given platform.

    Args:
        platform: One of 'weave', 'ema', 'zocdoc'.

    Returns:
        An initialized and validated client instance.

    Raises:
        AuthenticationError: If credentials are missing or expired.
        ValueError: If platform is unknown.
    """
    factories = {
        "weave": get_weave_client,
        "ema": get_ema_client,
        "zocdoc": get_zocdoc_client,
    }
    if platform not in factories:
        raise ValueError(f"Unknown platform: {platform}. Expected: {list(factories)}")
    return factories[platform]()


def check_all() -> dict:
    """Check session validity for all platforms.

    Returns dict mapping platform name to {"status": "valid"} or
    {"status": "expired", "error": "..."}.
    """
    results = {}
    for platform in ["weave", "ema", "zocdoc"]:
        try:
            get_client(platform)
            results[platform] = {"status": "valid"}
        except AuthenticationError as e:
            results[platform] = {"status": "expired", "error": str(e)}
        except Exception as e:
            results[platform] = {"status": "error", "error": str(e)}
    return results


def refresh_platform(platform: str) -> dict:
    """Refresh credentials for a platform via browser login.

    Requires playwright (install with: pip install liora-tools[auth]).
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    if platform == "weave":
        from liora_tools.auth.weave import login_browser
        token = login_browser()
        save_credentials("weave", {"token": token, "refreshed_at": now_iso})
        return {"status": "refreshed", "platform": "weave"}

    elif platform == "ema":
        from liora_tools.auth.ema import login_browser
        cookies = login_browser()
        save_credentials("ema", {"cookies": cookies, "last_verified": now_iso})
        return {"status": "refreshed", "platform": "ema"}

    elif platform == "zocdoc":
        from liora_tools.auth.zocdoc import login_browser
        cookies = login_browser()
        save_credentials("zocdoc", {"cookies": cookies, "last_verified": now_iso})
        return {"status": "refreshed", "platform": "zocdoc"}

    else:
        raise ValueError(f"Unknown platform: {platform}. Expected: weave, ema, zocdoc")
