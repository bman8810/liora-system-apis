"""Kernel Managed Auth bridge for Liora tool sessions.

Pulls cookies / JWT from a Kernel browser session that loads the Liora
profile (Managed Auth already AUTHENTICATED for zocdoc, weave, ema) and
writes them into the standard credential store used by session_manager.

Env (all optional except KERNEL_API_KEY for live sync):
  KERNEL_API_KEY          — required by `kernel` CLI
  KERNEL_PROJECT          — default gxujms2i14jyrds9w3dhrdok (Liora)
  KERNEL_LIORA_PROFILE    — default Liora (name) or profile id
  KERNEL_LIORA_TIMEOUT    — browser TTL seconds (default 180, max 600)
  LIORA_CREDENTIALS_DIR   — where cookies/JWT JSON is written
  EMA_BASE_URL            — override EmaConfig.base_url when host differs

PHI-safe: never log cookie values, JWT contents, or CDP URLs with tokens.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from liora_tools.auth.session_manager import save_credentials
from liora_tools.exceptions import AuthenticationError

log = logging.getLogger(__name__)

# Defaults for Kernel project Liora (see Hermes memory / investigation note).
DEFAULT_KERNEL_PROJECT = "gxujms2i14jyrds9w3dhrdok"
DEFAULT_PROFILE = "Liora"
DEFAULT_TIMEOUT = 180

# Managed Auth connection catalog (ids are stable; status is live-checked).
PLATFORM_CONNECTIONS: dict[str, dict[str, Any]] = {
    "zocdoc": {
        "domain": "zocdoc.com",
        "connection_id": "xd6n8zwiivoaaou2soiakkhw",
        "start_url": "https://www.zocdoc.com/practice/pt_FMyrNSVN50CbgjEI0NcL9h/dashboard",
        "cookie_domains": ("zocdoc.com",),
    },
    "weave": {
        "domain": "getweave.com",
        "connection_id": "r6si8mnlehee66e0jud325rt",
        "start_url": "https://app.getweave.com/home/dashboard",
        "cookie_domains": ("getweave.com", "weaveconnect.com"),
    },
    "ema": {
        "domain": "modmedapp.com",
        "connection_id": "lxrkfbmz57pxr8vjni6eybk6",
        # Kernel login targets modmedapp host; legacy clients used ema.md.
        "start_url": "https://lioraderm.modmedapp.com/ema/web/practice/staff/",
        "alt_urls": (
            "https://lioraderm.ema.md/ema/practice/staff/dashboard",
            "https://sso.ema.md/",
        ),
        "cookie_domains": (
            "modmedapp.com",
            "ema.md",
            "sso.ema.md",
            "lioraderm.modmedapp.com",
            "lioraderm.ema.md",
        ),
    },
}

PLATFORMS = tuple(PLATFORM_CONNECTIONS.keys())


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _kernel_bin() -> str:
    env = os.environ.get("KERNEL_CLI")
    if env and Path(env).exists():
        return env
    found = shutil.which("kernel")
    if found:
        return found
    # Common install on this Hermes host
    candidate = Path.home() / ".npm-global" / "bin" / "kernel"
    if candidate.exists():
        return str(candidate)
    raise AuthenticationError(
        "kernel CLI not found on PATH. Install @onkernel/cli or set KERNEL_CLI."
    )


def _project() -> str:
    return os.environ.get("KERNEL_PROJECT") or DEFAULT_KERNEL_PROJECT


def _profile() -> str:
    return os.environ.get("KERNEL_LIORA_PROFILE") or DEFAULT_PROFILE


def _timeout() -> int:
    raw = os.environ.get("KERNEL_LIORA_TIMEOUT", str(DEFAULT_TIMEOUT))
    try:
        n = int(raw)
    except ValueError:
        n = DEFAULT_TIMEOUT
    return max(60, min(n, 600))


def _run_kernel(args: list[str], *, timeout: int = 120) -> dict | list | str:
    """Run kernel CLI with JSON when possible. Never logs secrets."""
    cmd = [_kernel_bin(), *args]
    if "--project" not in args and "-p" not in args:
        # Prefer explicit project flag for multi-project safety
        cmd.extend(["--project", _project()])
    log.info("kernel %s", " ".join(_redact_cmd(args)))
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "KERNEL_PROJECT": _project()},
        )
    except subprocess.TimeoutExpired as e:
        raise AuthenticationError(f"kernel command timed out: {' '.join(_redact_cmd(args))}") from e
    except FileNotFoundError as e:
        raise AuthenticationError("kernel CLI not executable") from e

    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        # Strip possible JWT/cookie fragments from stderr before surfacing
        safe_err = _scrub_secrets(err or out or f"exit {proc.returncode}")
        raise AuthenticationError(f"kernel failed ({proc.returncode}): {safe_err[:400]}")

    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


def _redact_cmd(args: Iterable[str]) -> list[str]:
    return [a if not _looks_secret(a) else "<redacted>" for a in args]


def _looks_secret(s: str) -> bool:
    if len(s) > 80 and ("eyJ" in s or "jwt=" in s.lower()):
        return True
    return bool(re.search(r"(api[_-]?key|token|password|secret)=", s, re.I))


def _scrub_secrets(text: str) -> str:
    text = re.sub(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "<jwt>", text)
    text = re.sub(r"jwt=[^&\s\"']+", "jwt=<redacted>", text, flags=re.I)
    return text


def connection_status(platforms: Iterable[str] | None = None) -> dict[str, dict]:
    """Return Managed Auth connection status for Liora platforms (no secrets)."""
    wanted = set(platforms or PLATFORMS)
    raw = _run_kernel(["auth", "connections", "list", "-o", "json"], timeout=60)
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("connections") or raw.get("data") or []
    else:
        items = []
    if not isinstance(items, list):
        items = []
    by_id = {c.get("id"): c for c in items if isinstance(c, dict)}
    by_domain = {c.get("domain"): c for c in items if isinstance(c, dict)}

    out: dict[str, dict] = {}
    for name in PLATFORMS:
        if name not in wanted:
            continue
        meta = PLATFORM_CONNECTIONS[name]
        conn = by_id.get(meta["connection_id"]) or by_domain.get(meta["domain"])
        if not conn:
            out[name] = {
                "status": "missing",
                "domain": meta["domain"],
                "connection_id": meta["connection_id"],
                "ok": False,
                "hint": "Connection not found on Kernel project Liora",
            }
            continue
        status = conn.get("status") or "UNKNOWN"
        out[name] = {
            "status": status,
            "domain": conn.get("domain") or meta["domain"],
            "connection_id": conn.get("id") or meta["connection_id"],
            "can_reauth": bool(conn.get("can_reauth")),
            "last_auth_at": conn.get("last_auth_at"),
            "ok": status == "AUTHENTICATED",
            "error_code": conn.get("error_code"),
        }
    return out


def _create_browser(*, start_url: str | None = None) -> str:
    args = [
        "browsers", "create",
        "--profile-name", _profile(),
        "--stealth",
        "--timeout", str(_timeout()),
        "-o", "json",
    ]
    if start_url:
        args.extend(["--start-url", start_url])
    data = _run_kernel(args, timeout=90)
    if not isinstance(data, dict):
        raise AuthenticationError("kernel browsers create returned non-JSON")
    sid = data.get("session_id") or data.get("id")
    if not sid:
        raise AuthenticationError("kernel browsers create missing session_id")
    log.info("kernel browser session created id=%s", sid)
    return sid


def _delete_browser(session_id: str | None) -> None:
    if not session_id:
        return
    try:
        _run_kernel(["browsers", "delete", session_id], timeout=60)
        log.info("kernel browser session deleted id=%s", session_id)
    except Exception as e:
        log.warning("kernel browser delete failed id=%s err=%s", session_id, type(e).__name__)


def _playwright_execute(session_id: str, code: str, *, timeout: int = 90) -> str:
    """Run TS/JS in Kernel browser; return stdout text (may be JSON)."""
    # Prefer writing script to a temp file and passing via fs when large
    cmd = [
        _kernel_bin(),
        "browsers", "playwright", "execute", session_id,
        "--project", _project(),
        "--timeout", str(timeout),
        code,
    ]
    log.info("kernel playwright execute session=%s bytes=%d", session_id, len(code))
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout + 30,
        env={**os.environ, "KERNEL_PROJECT": _project()},
    )
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if proc.returncode != 0:
        raise AuthenticationError(
            f"playwright execute failed: {_scrub_secrets(err or out)[:400]}"
        )
    return out


def _extract_via_playwright(session_id: str, platform: str) -> dict[str, Any]:
    """Navigate (if needed) and extract cookies (+ weave JWT) via context APIs."""
    meta = PLATFORM_CONNECTIONS[platform]
    start = meta["start_url"]
    alts = list(meta.get("alt_urls") or [])
    domains_json = json.dumps(list(meta["cookie_domains"]))
    # Playwright execute: ESM only, `page` in scope. Write findings to /tmp then we
    # also return JSON from the expression when possible.
    code = f"""
const fs = await import("fs");
const domains = {domains_json};
const startUrl = {json.dumps(start)};
const altUrls = {json.dumps(alts)};
const platform = {json.dumps(platform)};

function domainMatch(cookieDomain, allowed) {{
  const d = (cookieDomain || "").replace(/^\\./, "").toLowerCase();
  return allowed.some(a => {{
    const aa = a.replace(/^\\./, "").toLowerCase();
    return d === aa || d.endsWith("." + aa) || aa.endsWith("." + d);
  }});
}}

async function safeGoto(url) {{
  try {{
    await page.goto(url, {{ waitUntil: "domcontentloaded", timeout: 45000 }});
    await page.waitForTimeout(1500);
    return page.url();
  }} catch (e) {{
    return "error:" + String(e).slice(0, 120);
  }}
}}

const nav = [];
nav.push(await safeGoto(startUrl));
for (const u of altUrls) {{
  nav.push(await safeGoto(u));
}}
// Return to primary for localStorage
await safeGoto(startUrl);

const all = await page.context().cookies();
const cookies = all
  .filter(c => domainMatch(c.domain, domains))
  .map(c => ({{
    name: c.name,
    value: c.value,
    domain: c.domain,
    path: c.path || "/",
    httpOnly: !!c.httpOnly,
    secure: !!c.secure,
    expires: c.expires,
  }}));

let token = null;
if (platform === "weave") {{
  try {{
    token = await page.evaluate(() => {{
      try {{ return localStorage.getItem("token"); }} catch {{ return null; }}
    }});
  }} catch {{}}
}}

const result = {{
  platform,
  url: page.url(),
  nav,
  cookie_count: cookies.length,
  cookie_names: cookies.map(c => c.name).sort(),
  cookies,
  token: token,
  has_datadome: cookies.some(c => c.name === "datadome"),
}};
fs.writeFileSync("/tmp/liora-kernel-extract.json", JSON.stringify(result));
return JSON.stringify({{
  platform: result.platform,
  url: result.url,
  cookie_count: result.cookie_count,
  cookie_names: result.cookie_names,
  has_datadome: result.has_datadome,
  has_token: !!(token && String(token).startsWith("eyJ")),
  // Include payload for CLI parse; caller must not log it
  cookies,
  token,
}});
"""
    raw = _playwright_execute(session_id, code, timeout=120)
    # Kernel may wrap output; find last JSON object
    payload = _parse_playwright_json(raw)
    if payload is None:
        # Fallback: pull file from browser VM
        payload = _read_extract_file(session_id)
    if not payload:
        raise AuthenticationError(
            f"Could not parse Kernel extract for {platform} (empty playwright output)"
        )
    return payload


def _parse_playwright_json(raw: str) -> dict | None:
    if not raw:
        return None
    # Try whole stdout
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            # Unwrap common CLI envelopes
            for key in ("result", "data", "output", "value"):
                if key in data and isinstance(data[key], (dict, str)):
                    inner = data[key]
                    if isinstance(inner, str):
                        try:
                            inner = json.loads(inner)
                        except json.JSONDecodeError:
                            pass
                    if isinstance(inner, dict) and (
                        "cookies" in inner or "token" in inner or "cookie_count" in inner
                    ):
                        return inner
            if "cookies" in data or "token" in data or "cookie_count" in data:
                return data
    except json.JSONDecodeError:
        pass
    # Scan for outermost JSON object containing cookie_count
    matches = list(re.finditer(r"\{[\s\S]*\}", raw))
    for m in reversed(matches):
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict) and (
                "cookies" in data or "cookie_count" in data or "token" in data
            ):
                return data
        except json.JSONDecodeError:
            continue
    return None


def _read_extract_file(session_id: str) -> dict | None:
    with tempfile.TemporaryDirectory(prefix="liora-kernel-") as td:
        dest = Path(td) / "extract.json"
        try:
            _run_kernel(
                [
                    "browsers", "fs", "read-file", session_id,
                    "--path", "/tmp/liora-kernel-extract.json",
                    "-o", str(dest),
                ],
                timeout=60,
            )
        except AuthenticationError:
            return None
        if not dest.exists():
            return None
        try:
            return json.loads(dest.read_text())
        except json.JSONDecodeError:
            return None


def _normalize_cookies(cookies: list) -> list[dict]:
    out = []
    for c in cookies or []:
        if not isinstance(c, dict) or "name" not in c or "value" not in c:
            continue
        out.append({
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain") or "",
            "path": c.get("path") or "/",
        })
    return out


def _save_platform(platform: str, payload: dict) -> dict:
    """Persist extract into credential store. Returns PHI-safe summary."""
    now = _now_iso()
    if platform == "weave":
        token = payload.get("token") or ""
        if isinstance(token, str):
            token = token.strip().strip('"')
        if not token or not str(token).startswith("eyJ"):
            raise AuthenticationError(
                "Weave: no JWT in localStorage from Kernel session "
                "(connection may be stale — reauth getweave.com on Liora profile)"
            )
        save_credentials("weave", {
            "token": token,
            "refreshed_at": now,
            "source": "kernel_liora",
        })
        return {
            "status": "saved",
            "platform": "weave",
            "source": "kernel_liora",
            "refreshed_at": now,
            "token_prefix": token[:8] + "…",
        }

    cookies = _normalize_cookies(payload.get("cookies") or [])
    if not cookies:
        raise AuthenticationError(
            f"{platform}: no cookies extracted from Kernel session "
            f"(need AUTHENTICATED Managed Auth on Liora profile)"
        )

    if platform == "zocdoc":
        names = {c["name"] for c in cookies}
        save_credentials("zocdoc", {
            "cookies": cookies,
            "last_verified": now,
            "source": "kernel_liora",
        })
        summary = {
            "status": "saved",
            "platform": "zocdoc",
            "source": "kernel_liora",
            "refreshed_at": now,
            "cookie_count": len(cookies),
            "cookie_names": sorted(names),
        }
        if "datadome" not in names:
            summary["warning"] = "No datadome cookie — API may be blocked"
        return summary

    if platform == "ema":
        names = {c["name"] for c in cookies}
        # Prefer modmedapp host for subsequent client calls when those cookies dominate
        domains = {c.get("domain", "") for c in cookies}
        preferred_base = None
        if any("modmedapp.com" in d for d in domains):
            preferred_base = "https://lioraderm.modmedapp.com"
        elif any("ema.md" in d for d in domains):
            preferred_base = "https://lioraderm.ema.md"
        data = {
            "cookies": cookies,
            "last_verified": now,
            "source": "kernel_liora",
        }
        if preferred_base:
            data["base_url"] = preferred_base
        save_credentials("ema", data)
        return {
            "status": "saved",
            "platform": "ema",
            "source": "kernel_liora",
            "refreshed_at": now,
            "cookie_count": len(cookies),
            "cookie_names": sorted(names),
            "base_url": preferred_base,
        }

    raise ValueError(f"Unknown platform: {platform}")


def sync_platform(platform: str, *, require_authenticated: bool = True) -> dict:
    """Create Kernel Liora browser, extract auth for one platform, save, delete browser."""
    if platform not in PLATFORM_CONNECTIONS:
        raise ValueError(f"Unknown platform: {platform}. Expected: {list(PLATFORMS)}")

    status = connection_status([platform]).get(platform, {})
    if require_authenticated and not status.get("ok"):
        raise AuthenticationError(
            f"Kernel Managed Auth for {platform} is not AUTHENTICATED "
            f"(status={status.get('status')!r}). "
            f"Re-auth connection {status.get('connection_id')} on project Liora, "
            f"then retry: python -m liora_tools auth kernel-sync {platform}"
        )

    meta = PLATFORM_CONNECTIONS[platform]
    sid = None
    try:
        sid = _create_browser(start_url=meta["start_url"])
        # Brief settle for profile cookies to attach
        time.sleep(2)
        payload = _extract_via_playwright(sid, platform)
        summary = _save_platform(platform, payload)
        summary["connection"] = {
            k: status.get(k) for k in ("status", "connection_id", "domain", "can_reauth")
        }
        summary["url"] = payload.get("url")
        return summary
    finally:
        _delete_browser(sid)


def sync_all(
    platforms: Iterable[str] | None = None,
    *,
    require_authenticated: bool = True,
) -> dict[str, dict]:
    """Sync one or more platforms. Each platform gets its own short browser session."""
    targets = list(platforms or PLATFORMS)
    results: dict[str, dict] = {}
    for name in targets:
        try:
            results[name] = sync_platform(name, require_authenticated=require_authenticated)
        except Exception as e:
            results[name] = {
                "status": "error",
                "platform": name,
                "error": _scrub_secrets(str(e))[:500],
                "error_type": type(e).__name__,
            }
    return results


def ensure_credentials(
    platform: str,
    *,
    force: bool = False,
    max_age_seconds: int | None = None,
) -> dict:
    """Ensure local credentials exist; refresh from Kernel if missing/stale/forced.

    max_age_seconds: if set, re-sync when credential timestamp older than this.
    """
    from liora_tools.auth.session_manager import load_credentials

    if platform not in PLATFORMS:
        raise ValueError(f"Unknown platform: {platform}")

    if not force:
        creds = load_credentials(platform)
        if creds:
            ts_key = "refreshed_at" if platform == "weave" else "last_verified"
            ts = creds.get(ts_key)
            fresh = True
            if max_age_seconds is not None and ts:
                try:
                    # support Z-suffix
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds()
                    fresh = age <= max_age_seconds
                except ValueError:
                    fresh = False
            if fresh and (
                (platform == "weave" and creds.get("token"))
                or (platform != "weave" and creds.get("cookies"))
            ):
                return {
                    "status": "cached",
                    "platform": platform,
                    "source": creds.get("source", "local"),
                    ts_key: ts,
                }

    return sync_platform(platform)


def kernel_available() -> dict:
    """Cheap readiness check (no browser create)."""
    info: dict[str, Any] = {
        "kernel_cli": False,
        "kernel_api_key": bool(os.environ.get("KERNEL_API_KEY")),
        "project": _project(),
        "profile": _profile(),
    }
    try:
        info["kernel_cli"] = bool(_kernel_bin())
        info["kernel_path"] = _kernel_bin()
    except AuthenticationError as e:
        info["error"] = str(e)
        return info
    try:
        info["connections"] = connection_status()
    except AuthenticationError as e:
        info["error"] = _scrub_secrets(str(e))[:300]
    return info
