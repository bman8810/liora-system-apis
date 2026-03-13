"""Liora Voice Agent — entry point.

Usage:
    python -m voice_agent --dial 3302067819          # uses WEAVE_TOKEN from .env
    python -m voice_agent --token <jwt> --dial 3302067819
    python -m voice_agent                            # wait for incoming call
"""

import argparse
import asyncio
import logging
import sys

from . import config
from .call_manager import CallManager


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
        description="Liora Voice Agent — SIP softphone + Grok realtime bridge"
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
        "-v", "--verbose", action="store_true",
        help="Enable debug logging"
    )
    args = parser.parse_args()

    setup_logging(args.verbose)

    token = args.token or config.WEAVE_TOKEN
    if not token:
        print("ERROR: No token provided. Use --token <jwt> or set WEAVE_TOKEN in .env")
        sys.exit(1)

    manager = CallManager(token=token, destination=args.dial or "")

    try:
        asyncio.run(manager.run())
    except KeyboardInterrupt:
        print("\nInterrupted — shutting down")
        sys.exit(0)


if __name__ == "__main__":
    main()
