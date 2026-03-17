"""Refresh Weave JWT by reading from the token drop file or stdin.

The token drop file (~/.liora/weave_token.txt) is populated by a
Claude Code cron that extracts the token from Chrome via
Claude-in-Chrome every 20 minutes.

This script can also accept a token on stdin for manual refresh:
    echo "eyJ..." | python3 -m liora_tools.scripts.refresh_weave_token

Usage:
    python3 -m liora_tools.scripts.refresh_weave_token           # check status
    python3 -m liora_tools.scripts.refresh_weave_token --write TOKEN  # write token
"""

import base64
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

TOKEN_FILE = Path(os.path.expanduser("~/.liora/weave_token.txt"))


def validate_token(token):
    """Returns (valid, remaining_seconds)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (4 - len(payload) % 4)
        d = json.loads(base64.b64decode(payload))
        remaining = d["exp"] - datetime.now(timezone.utc).timestamp()
        return remaining > 0, remaining
    except Exception:
        return False, 0


def write_token(token):
    """Write token to the drop file."""
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token.strip())


def read_token():
    """Read token from the drop file."""
    if not TOKEN_FILE.exists():
        return None
    token = TOKEN_FILE.read_text().strip()
    return token if token.startswith("eyJ") else None


def main():
    # --write mode: write a token to the drop file
    if len(sys.argv) >= 3 and sys.argv[1] == "--write":
        token = sys.argv[2]
        valid, remaining = validate_token(token)
        if not valid:
            print("Invalid or expired token", file=sys.stderr)
            return 1
        write_token(token)
        print(f"Written ({int(remaining / 60)}m remaining)")
        return 0

    # stdin mode: read token from pipe
    if not sys.stdin.isatty():
        token = sys.stdin.read().strip()
        if token.startswith("eyJ"):
            valid, remaining = validate_token(token)
            if valid:
                write_token(token)
                print(f"Written from stdin ({int(remaining / 60)}m remaining)")
                return 0
            print("Token from stdin is expired", file=sys.stderr)
            return 1

    # Status check mode
    token = read_token()
    if token:
        valid, remaining = validate_token(token)
        if valid:
            print(f"Token valid ({int(remaining / 60)}m remaining)")
            return 0
        print("Token expired")
        return 1

    print("No token file found at", TOKEN_FILE)
    return 1


if __name__ == "__main__":
    sys.exit(main())
