"""python -m twilio_sink"""

from __future__ import annotations

import os

import uvicorn

from twilio_sink.config import settings


def main() -> None:
    host = settings.twilio_sink_host
    # Prefer TWILIO_SINK_PORT over generic PORT (Hermes/.env often sets PORT for other apps).
    port = int(
        os.environ.get("TWILIO_SINK_PORT")
        or settings.twilio_sink_port
        or os.environ.get("PORT")
        or 8090
    )
    uvicorn.run(
        "twilio_sink.app:app",
        host=host,
        port=port,
        log_level="info",
        ws="websockets",
    )


if __name__ == "__main__":
    main()
