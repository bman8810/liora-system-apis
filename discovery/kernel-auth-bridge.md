# Kernel Liora auth bridge

Bridge Managed Auth on Kernel project **Liora** into local cookies/JWT that
`liora_tools` clients already consume.

## Why

| Path | Use |
|------|-----|
| **Kernel bridge (preferred on Hermes)** | Profile + Managed Auth stay logged in; extract at runtime |
| Local Playwright `auth refresh` | Fallback when Kernel unavailable |
| Windows Chrome `auth save-chrome` | Laptop / Claude-for-Chrome path |

Connections (project Liora, profile `Liora`):

| Platform | Domain | Typical status |
|----------|--------|----------------|
| Zocdoc | zocdoc.com | AUTHENTICATED |
| Weave | getweave.com | AUTHENTICATED |
| EMA ModMed | modmedapp.com | AUTHENTICATED |
| Outlook | live.com | often NEEDS_AUTH (Hosted UI) — not required for tools API |

## Env

```bash
export KERNEL_API_KEY=...                 # kernel CLI
export KERNEL_PROJECT=gxujms2i14jyrds9w3dhrdok
export KERNEL_LIORA_PROFILE=Liora         # or profile id
export KERNEL_LIORA_TIMEOUT=180           # browser TTL seconds (60–600)
export LIORA_CREDENTIALS_DIR=~/.liora/credentials
export EMA_BASE_URL=https://lioraderm.modmedapp.com
```

Credential files written (never commit):

- `weave_token.json` — `{token, refreshed_at, source}`
- `ema_cookies.json` — `{cookies, last_verified, source, base_url?}`
- `zocdoc_cookies.json` — `{cookies, last_verified, source}`

## CLI

```bash
# Readiness (no browser $)
python -m liora_tools auth kernel-status

# Pull one platform (creates Kernel browser, extracts, deletes session)
python -m liora_tools auth kernel-sync weave
python -m liora_tools auth kernel-sync ema
python -m liora_tools auth kernel-sync zocdoc

# All three
python -m liora_tools auth kernel-sync all

# Validate clients against live APIs
python -m liora_tools auth check
```

`auth refresh <platform>` tries Kernel first, then local Playwright.

`get_client("weave"|"ema"|"zocdoc")` auto-attempts Kernel sync when local
creds are missing or fail validation.

## Python

```python
from liora_tools.auth import kernel_bridge
from liora_tools.auth.session_manager import get_client

kernel_bridge.kernel_available()          # status only
kernel_bridge.sync_platform("weave")      # save JWT
client = get_client("weave")              # validated WeaveClient
```

## Safety

- **Always deletes** Kernel browser sessions after extract (billing).
- Logs never include JWT/cookie values or CDP URLs with tokens.
- Do not commit `~/.liora/credentials/*` or genie `*-auth.json`.
- PHI: smoke checks use list/count endpoints only — no patient dumps.

## Ops notes

1. If `kernel-status` shows `NEEDS_AUTH`, re-run Managed Auth login on that
   connection (Hosted UI for Outlook/MFA-hard paths). Tools cannot invent
   sessions.
2. Multi-project: always set `KERNEL_PROJECT` (Liora ≠ default Galatiq project).
3. EMA host: Kernel login is `lioraderm.modmedapp.com`. Default `EmaConfig.base_url`
   matches; set `EMA_BASE_URL` if a practice still uses `*.ema.md`.
4. Zocdoc needs a fresh `datadome` cookie; warning is returned if missing.
5. Weave JWT lives in `localStorage.token` on `app.getweave.com` (~hours).
