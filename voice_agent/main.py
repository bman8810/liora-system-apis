"""Liora Voice Agent — entry point (voice product B).

Usage:
    python -m voice_agent --verify-binding            # read-only config check
    python -m voice_agent --dial-lab-sink             # dial Twilio sink C DID
    python -m voice_agent --dial 3302067819           # WEAVE_TOKEN from .env
    python -m voice_agent --token <jwt> --dial 3302067819
    python -m voice_agent                             # wait for incoming call
"""

import argparse
import asyncio
import json
import logging
import sys

from . import config
from .auth import get_session, verify_voice_b_binding


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # Quiet down noisy libs
    logging.getLogger("websockets").setLevel(logging.WARNING)


def main():
    parser = argparse.ArgumentParser(
        description="Liora Voice Agent B — Barric SIP 7002 + Grok realtime bridge"
    )
    parser.add_argument(
        "--token",
        help="Weave JWT token (defaults to WEAVE_TOKEN from .env)"
    )
    parser.add_argument(
        "--dial",
        help="Phone number to dial (digits only, e.g. 3302067819)"
    )
    parser.add_argument(
        "--dial-lab-sink",
        action="store_true",
        help=f"Dial Twilio sink C ({config.TWILIO_SINK_DID}) for B→C lab",
    )
    parser.add_argument(
        "--verify-binding",
        action="store_true",
        help="Read-only: confirm Barric 7002 + DID allowlist + outbound CLI (no call)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable debug logging"
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    token = args.token or config.WEAVE_TOKEN
    if not token:
        print("ERROR: No token provided. Use --token <jwt> or set WEAVE_TOKEN in .env")
        sys.exit(1)

    if args.verify_binding:
        session = get_session(token)
        evidence = verify_voice_b_binding(session)
        print(json.dumps(evidence, indent=2, default=str))
        ok = (
            evidence.get("sip_profile_is_barric_7002")
            and evidence.get("sip_profile_is_not_genie")
            and evidence.get("lab_sink_did", {}).get("in_allowlist")
            and evidence.get("outbound_cli", {}).get("on_weave_accessible")
        )
        sys.exit(0 if ok else 2)

    # Lazy import: call stack needs audioop/aiortc (broken on stock Python 3.13).
    from .call_manager import CallManager

    destination = args.dial or ""
    if args.dial_lab_sink:
        destination = config.TWILIO_SINK_DID

    manager = CallManager(token=token, destination=destination)

    try:
        asyncio.run(manager.run())
    except KeyboardInterrupt:
        print("\nInterrupted — shutting down")
        sys.exit(0)


if __name__ == "__main__":
    main()
