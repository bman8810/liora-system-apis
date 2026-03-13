"""Grok Realtime API WebSocket client.

Connects to Grok's bidirectional audio WebSocket for real-time voice conversation.
Sends/receives G.711 μ-law audio encoded as base64.
"""

import asyncio
import base64
import json
import logging
from typing import Callable, Optional

import websockets

from . import config

logger = logging.getLogger(__name__)


class GrokBridge:
    """Client for Grok's realtime voice API."""

    def __init__(
        self,
        api_key: str = "",
        on_audio: Optional[Callable] = None,
        on_speech_started: Optional[Callable] = None,
        on_speech_stopped: Optional[Callable] = None,
        on_response_done: Optional[Callable] = None,
        on_transcript: Optional[Callable] = None,
    ):
        self.api_key = api_key or config.GROK_API_KEY
        if not self.api_key:
            raise ValueError("XAI_API_KEY environment variable not set")

        self.on_audio = on_audio                    # async callback(mulaw_bytes)
        self.on_speech_started = on_speech_started  # async callback()
        self.on_speech_stopped = on_speech_stopped  # async callback()
        self.on_response_done = on_response_done    # async callback(dict)
        self.on_transcript = on_transcript          # async callback(str, role)

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._session_ready: Optional[asyncio.Event] = None

    async def connect(self):
        """Connect to Grok realtime WebSocket."""
        self._session_ready = asyncio.Event()
        url = config.GROK_REALTIME_URL
        headers = {
            "Authorization": f"Bearer {self.api_key}",
        }

        logger.info(f"Connecting to Grok Realtime at {url}...")
        self.ws = await websockets.connect(
            url,
            additional_headers=headers,
            ping_interval=30,
            ping_timeout=10,
            max_size=10 * 1024 * 1024,  # 10MB for audio chunks
        )
        logger.info("Grok WebSocket connected")

    async def configure_session(self):
        """Send session.update to configure voice, audio format, and system instructions."""
        session_config = {
            "type": "session.update",
            "session": {
                "voice": config.GROK_VOICE,
                "temperature": 0.9,
                "instructions": config.SYSTEM_INSTRUCTIONS,
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.3,
                    "silence_duration_ms": 400,
                },
                "audio": {
                    "input": {"format": {"type": "audio/pcmu"}},
                    "output": {"format": {"type": "audio/pcmu"}},
                },
            },
        }

        logger.info("Sending session.update to Grok")
        await self.ws.send(json.dumps(session_config))

    async def send_audio(self, mulaw_bytes: bytes):
        """Send μ-law audio to Grok via input_audio_buffer.append."""
        if not self.ws:
            return

        encoded = base64.b64encode(mulaw_bytes).decode("ascii")
        msg = {
            "type": "input_audio_buffer.append",
            "audio": encoded,
        }
        await self.ws.send(json.dumps(msg))

    async def commit_audio(self):
        """Commit the audio buffer (signal end of user speech)."""
        if not self.ws:
            return

        msg = {"type": "input_audio_buffer.commit"}
        await self.ws.send(json.dumps(msg))

    async def send_response_create(self):
        """Send response.create to trigger Grok's initial greeting."""
        if not self.ws:
            return

        logger.info("Triggering initial Grok greeting (response.create)")
        msg = {"type": "response.create"}
        await self.ws.send(json.dumps(msg))

    async def cancel_response(self):
        """Cancel the current response (for interruption handling)."""
        if not self.ws:
            return

        msg = {"type": "response.cancel"}
        await self.ws.send(json.dumps(msg))

    async def truncate_audio(self):
        """Truncate the output audio buffer (for interruption handling)."""
        if not self.ws:
            return

        msg = {"type": "output_audio_buffer.clear"}
        await self.ws.send(json.dumps(msg))

    async def run(self):
        """Main event processing loop for Grok WebSocket."""
        self._running = True
        try:
            async for raw_message in self.ws:
                try:
                    event = json.loads(raw_message)
                except json.JSONDecodeError:
                    logger.warning(f"Non-JSON message from Grok: {raw_message[:100]}")
                    continue

                await self._dispatch(event)
        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"Grok WebSocket closed: {e}")
        finally:
            self._running = False

    async def _dispatch(self, event: dict):
        """Route Grok events to handlers."""
        event_type = event.get("type", "")

        if event_type == "session.created":
            logger.info("Grok session created")

        elif event_type == "session.updated":
            logger.info("Grok session configured")
            self._session_ready.set()

        elif event_type == "response.output_audio.delta":
            # Outbound audio from Grok
            audio_b64 = event.get("delta", "")
            if audio_b64 and self.on_audio:
                mulaw_bytes = base64.b64decode(audio_b64)
                if not hasattr(self, '_audio_delta_count'):
                    self._audio_delta_count = 0
                self._audio_delta_count += 1
                if self._audio_delta_count == 1:
                    logger.info(f"First Grok audio delta: {len(mulaw_bytes)} bytes")
                elif self._audio_delta_count % 100 == 0:
                    logger.debug(f"Grok audio deltas received: {self._audio_delta_count}")
                await self.on_audio(mulaw_bytes)

        elif event_type == "input_audio_buffer.speech_started":
            logger.info("Grok detected speech start")
            if self.on_speech_started:
                await self.on_speech_started()

        elif event_type == "input_audio_buffer.speech_stopped":
            logger.info("Grok detected speech stop")
            if self.on_speech_stopped:
                await self.on_speech_stopped()

        elif event_type == "response.done":
            logger.info("Grok response complete")
            if self.on_response_done:
                await self.on_response_done(event)

        elif event_type == "response.output_audio.done":
            logger.debug("Grok audio output complete")

        elif event_type == "response.audio_transcript.delta":
            # Real-time transcript of Grok's speech
            text = event.get("delta", "")
            if text and self.on_transcript:
                await self.on_transcript(text, "assistant")

        elif event_type == "conversation.item.input_audio_transcription.completed":
            # Transcript of user's speech
            text = event.get("transcript", "")
            if text and self.on_transcript:
                await self.on_transcript(text, "user")

        elif event_type == "error":
            error = event.get("error", {})
            logger.error(f"Grok error: {error.get('message', event)}")

        elif event_type == "response.created":
            logger.info("Grok response created")

        elif event_type in ("response.output_item.added",
                            "response.content_part.added", "response.content_part.done",
                            "response.output_item.done", "rate_limits.updated",
                            "input_audio_buffer.committed"):
            logger.debug(f"Grok event: {event_type}")

        else:
            logger.debug(f"Unhandled Grok event: {event_type}")

    async def close(self):
        """Close the Grok WebSocket."""
        self._running = False
        if self.ws:
            await self.ws.close()
            logger.info("Grok WebSocket closed")
