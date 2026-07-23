"""Env-only config for Twilio sink C. Never log secret values."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_artifact_dir() -> str:
    # Prefer durable Liora cache; fall back under /tmp for throwaway runs.
    for candidate in (
        os.environ.get("TWILIO_SINK_ARTIFACT_DIR"),
        "/opt/data/workspace/liora/cache/twilio-sink",
        "/tmp/liora-twilio-sink",
    ):
        if candidate:
            return candidate
    return "/tmp/liora-twilio-sink"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    # Public base URL Twilio hits (https://… no trailing slash). Required for TwiML stream URL.
    twilio_sink_public_base: str = ""
    # Bind
    twilio_sink_host: str = "0.0.0.0"
    twilio_sink_port: int = 8090
    # Hold time after greet so stream/recording can run during probe
    twilio_sink_hold_seconds: int = 25
    # Max call recording length (TwiML Record)
    twilio_sink_record_max_seconds: int = 60
    # Optional shared secret for /voice/admin* (empty = open on private host)
    twilio_sink_admin_token: str = ""
    # Artifacts (JSON call logs; recording binaries stay at Twilio URLs unless downloaded)
    twilio_sink_artifact_dir: str = _default_artifact_dir()

    # Twilio REST (env only — also accept Hermes-standard names)
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_api_key_sid: str = ""
    twilio_api_key_secret: str = ""
    twilio_phone_number: str = ""

    # Optional: validate X-Twilio-Signature (needs auth token, not only API key)
    twilio_validate_signature: bool = False

    # AI on media stream: none | grok
    twilio_sink_ai: str = "grok"
    # Grok Realtime (XAI_API_KEY also read from env by grok_bridge)
    xai_api_key: str = ""
    grok_voice: str = "Ara"
    # TwiML mode: connect = bidirectional <Connect><Stream>; start_record = Start+Record smoke
    twilio_sink_twiml_mode: str = "connect"
    # Max seconds to keep Connect stream open (Pause after Start mode)
    twilio_sink_connect_seconds: int = 90

    def public_base(self) -> str:
        base = (self.twilio_sink_public_base or "").rstrip("/")
        return base

    def has_rest_auth(self) -> bool:
        if self.twilio_account_sid and self.twilio_auth_token:
            return True
        if self.twilio_account_sid and self.twilio_api_key_sid and self.twilio_api_key_secret:
            return True
        return False

    def rest_basic_auth(self) -> tuple[str, str]:
        """Return (username, password) for Twilio REST Basic auth."""
        if self.twilio_api_key_sid and self.twilio_api_key_secret:
            return self.twilio_api_key_sid, self.twilio_api_key_secret
        if self.twilio_auth_token and self.twilio_account_sid:
            return self.twilio_account_sid, self.twilio_auth_token
        raise RuntimeError("Twilio REST auth missing (need API key SID+secret or auth token)")

    def ensure_artifact_dir(self) -> Path:
        p = Path(self.twilio_sink_artifact_dir)
        p.mkdir(parents=True, exist_ok=True)
        (p / "calls").mkdir(exist_ok=True)
        (p / "recordings").mkdir(exist_ok=True)
        (p / "streams").mkdir(exist_ok=True)
        return p


settings = Settings()
