# Auth Refresh Guide — Chrome-Based Credential Extraction

## Overview

Authentication for Modmed, Weave, and Zocdoc requires a real Chrome browser
(bot detection, MFA, Outlook access). This runs on **Windows** via Claude for
Chrome, and credentials are saved to a shared directory accessible by WSL2.

## Shared Credential Directory

| Environment | Path |
|---|---|
| **Windows** | `C:\Users\barri\.liora\credentials\` (default) |
| **WSL2** | `/mnt/c/Users/barri/.liora/credentials/` (via env var) |

### WSL2 Setup

Add to `~/.bashrc` or `~/.profile` on WSL2:

```bash
export LIORA_CREDENTIALS_DIR=/mnt/c/Users/barri/.liora/credentials
```

### Credential Files

| File | Platform | Contents |
|---|---|---|
| `weave_token.json` | Weave | `{"token": "eyJ...", "refreshed_at": "..."}` |
| `ema_cookies.json` | Modmed EMA | `{"cookies": [...], "last_verified": "..."}` |
| `zocdoc_cookies.json` | Zocdoc | `{"cookies": [...], "last_verified": "..."}` |

---

## Refresh Workflows (Claude for Chrome on Windows)

### Weave (JWT Token)

1. **Navigate** to `https://app.getweave.com/sign-in`
2. **Fill email** → Enter → wait for password field
3. **Fill password** → Enter
4. **Handle MFA** if prompted (6-digit code from Outlook email "Weave Login Code")
5. **Wait** for redirect to `/home/dashboard`
6. **Extract token**:
   ```javascript
   // Execute via javascript_tool on the app.getweave.com tab
   localStorage.getItem('token')
   ```
7. **Save** (via Bash):
   ```bash
   echo '"eyJ..."' | python -m liora_tools auth save-chrome weave
   ```

### Modmed EMA (Session Cookies)

1. **Navigate** to `https://lioraderm.ema.md/ema/Login.action`
2. **Click** "Continue as Practice Staff"
3. **Fill username** (`breed`) and **password** on Keycloak form
4. **Wait** for redirect to `practice/staff/dashboard`
5. **Extract cookies**:
   ```javascript
   // Execute via javascript_tool on the lioraderm.ema.md tab
   document.cookie.split(';').map(c => {
       const [name, ...rest] = c.trim().split('=');
       return {name, value: rest.join('='), domain: location.hostname, path: '/'};
   })
   ```
6. **Also extract SSO cookies** (navigate to `https://sso.ema.md` briefly):
   ```javascript
   document.cookie.split(';').map(c => {
       const [name, ...rest] = c.trim().split('=');
       return {name, value: rest.join('='), domain: location.hostname, path: '/'};
   })
   ```
7. **Merge and save** (combine both cookie arrays):
   ```bash
   echo '[...ema_cookies, ...sso_cookies]' | python -m liora_tools auth save-chrome ema
   ```

**Note**: If `document.cookie` misses httpOnly session cookies (EMA returns 302
instead of 200), you may need to use `browser_cookie3` or `rookiepy` to read
cookies directly from Chrome's cookie database. Install with:
```bash
pip install rookiepy
```
Then extract:
```python
import rookiepy
cookies = rookiepy.chrome(["lioraderm.ema.md", "sso.ema.md"])
```

### Zocdoc (Cookies + DataDome)

1. **Navigate** to `https://www.zocdoc.com/signin?provider=1`
2. **Fill email** → Submit → wait for password field
3. **Fill password** → Submit
4. **Wait** for redirect to `/practice/*/dashboard`
5. **Extract cookies**:
   ```javascript
   // Execute via javascript_tool on the zocdoc.com tab
   document.cookie.split(';').map(c => {
       const [name, ...rest] = c.trim().split('=');
       return {name, value: rest.join('='), domain: location.hostname, path: '/'};
   })
   ```
6. **Save**:
   ```bash
   echo '[...]' | python -m liora_tools auth save-chrome zocdoc
   ```

**Note**: The `datadome` cookie is critical for API access. If it's httpOnly
and not in `document.cookie`, use `rookiepy`:
```python
import rookiepy
cookies = rookiepy.chrome(["zocdoc.com", ".zocdoc.com", "api2.zocdoc.com"])
```

---

## Checking Token Freshness

```bash
# Check all platforms
python -m liora_tools auth check

# Output: {"weave": {"status": "valid"}, "ema": {...}, "zocdoc": {...}}
```

## Routine Refresh

For automated/scheduled refresh, create a Windows Task Scheduler task that
runs periodically (e.g., every 4 hours) to check and alert when tokens expire:

```powershell
# check-auth.ps1
$result = python -m liora_tools auth check 2>$null | ConvertFrom-Json
$expired = @()
foreach ($platform in @("weave", "ema", "zocdoc")) {
    if ($result.$platform.status -ne "valid") {
        $expired += $platform
    }
}
if ($expired.Count -gt 0) {
    # Send notification (toast, email, etc.)
    [System.Windows.Forms.MessageBox]::Show(
        "Auth expired for: $($expired -join ', ')",
        "Liora Auth Alert"
    )
}
```

Or have Claude Code check on startup — add to CLAUDE.md:
```
When starting a session that uses liora_tools, run `python -m liora_tools auth check`
first. If any platform shows expired, offer to refresh via Chrome.
```

---

## Platform-Specific Notes

### Weave
- Token is a JWT in localStorage — no httpOnly issues
- MFA code comes via Outlook email — Claude for Chrome can read Outlook
- Token typically lasts several hours

### EMA (Modmed)
- Uses Keycloak SSO — cookies from both `lioraderm.ema.md` and `sso.ema.md`
- Session expiry returns 302 (not 401)
- SSO cookies enable faster re-auth (tier 2 refresh)

### Zocdoc
- DataDome anti-bot protection — real Chrome required for login
- The `datadome` cookie is essential for API calls
- Cookie-based `RequestsTransport` works from WSL2 when datadome cookie is fresh
- If DataDome blocks requests-based transport, API calls must go through Windows
