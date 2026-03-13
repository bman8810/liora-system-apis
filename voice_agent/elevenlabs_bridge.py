"""ElevenLabs Conversational AI WebSocket client.

Connects to ElevenLabs' conversational AI agent via signed WebSocket URL.
Handles audio format conversion between G.711 μ-law 8kHz (phone) and
PCM16 16kHz (ElevenLabs).
"""

import asyncio
import audioop
import base64
import json
import logging
from typing import Callable, Optional

import websockets

from . import config
from .ai_bridge import AIBridge

logger = logging.getLogger(__name__)

# Resampling constants
_INPUT_RATE = config.PCMU_SAMPLE_RATE   # 8000
_OUTPUT_RATE = config.ELEVENLABS_SAMPLE_RATE  # 16000
_CHUNK_MS = 100  # Accumulate ~100ms of audio before sending
_CHUNK_BYTES_8K = (_INPUT_RATE * 2 * _CHUNK_MS) // 1000  # PCM16 bytes at 8kHz for 100ms = 1600


class ElevenLabsBridge(AIBridge):
    """Client for ElevenLabs Conversational AI."""

    def __init__(
        self,
        on_audio: Optional[Callable] = None,
        on_speech_started: Optional[Callable] = None,
        on_speech_stopped: Optional[Callable] = None,
        on_response_done: Optional[Callable] = None,
        on_transcript: Optional[Callable] = None,
    ):
        super().__init__(
            on_audio=on_audio,
            on_speech_started=on_speech_started,
            on_speech_stopped=on_speech_stopped,
            on_response_done=on_response_done,
            on_transcript=on_transcript,
        )

        if not config.ELEVENLABS_API_KEY:
            raise ValueError("ELEVENLABS_API_KEY environment variable not set")
        if not config.ELEVENLABS_AGENT_ID:
            raise ValueError("ELEVENLABS_AGENT_ID environment variable not set")

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False

        # Audio buffering for inbound (phone → ElevenLabs)
        self._inbound_buf = b""
        self._upsample_state = None  # audioop.ratecv state for continuous resampling

        # Downsampling state for outbound (ElevenLabs → phone)
        self._downsample_state = None

        # Interruption tracking by event_id
        self._last_interrupt_event_id = 0
        self._current_event_id = 0

        # Patient name for session config
        self._patient_name = "the patient"

    async def connect(self):
        """Get signed URL from ElevenLabs API and connect to WebSocket."""
        from elevenlabs import ElevenLabs

        client = ElevenLabs(api_key=config.ELEVENLABS_API_KEY)

        logger.info("Requesting signed URL from ElevenLabs...")
        signed_url_response = client.conversational_ai.conversations.get_signed_url(
            agent_id=config.ELEVENLABS_AGENT_ID,
        )
        signed_url = signed_url_response.signed_url

        logger.info("Connecting to ElevenLabs Conversational AI...")
        self.ws = await websockets.connect(
            signed_url,
            max_size=10 * 1024 * 1024,
        )
        logger.info("ElevenLabs WebSocket connected")

    async def configure_session(self, patient_name: str = "the patient"):
        """Send conversation_initiation_client_data with config overrides."""
        self._patient_name = patient_name
        instructions = config.SYSTEM_INSTRUCTIONS.format(patient_name=patient_name)

        init_msg = {
            "type": "conversation_initiation_client_data",
        }

        logger.info("Sending conversation_initiation_client_data to ElevenLabs")
        await self.ws.send(json.dumps(init_msg))

    async def send_audio(self, mulaw_bytes: bytes):
        """Convert μ-law 8kHz → PCM16 16kHz and send to ElevenLabs.

        Accumulates audio into ~100ms chunks before sending to reduce
        WebSocket message overhead.
        """
        if not self.ws:
            return

        # μ-law → PCM16 at 8kHz
        pcm_8k = audioop.ulaw2lin(mulaw_bytes, 2)

        # Accumulate into buffer
        self._inbound_buf += pcm_8k

        # Send when we have enough
        if len(self._inbound_buf) >= _CHUNK_BYTES_8K:
            chunk = self._inbound_buf
            self._inbound_buf = b""

            # Resample 8kHz → 16kHz with continuous state
            pcm_16k, self._upsample_state = audioop.ratecv(
                chunk, 2, 1, _INPUT_RATE, _OUTPUT_RATE, self._upsample_state,
            )

            encoded = base64.b64encode(pcm_16k).decode("ascii")
            msg = {"user_audio_chunk": encoded}

            if not hasattr(self, '_send_count'):
                self._send_count = 0
            self._send_count += 1
            if self._send_count == 1:
                logger.info(f"First audio chunk to ElevenLabs: {len(pcm_16k)} bytes PCM16@16kHz")
            elif self._send_count % 50 == 0:
                logger.info(f"Audio chunks sent to ElevenLabs: {self._send_count}")

            await self.ws.send(json.dumps(msg))

    async def commit_audio(self):
        """No-op — ElevenLabs handles VAD server-side."""
        pass

    async def send_response_create(self):
        """No-op — ElevenLabs fires first_message automatically on connect."""
        pass

    async def cancel_response(self):
        """No-op — ElevenLabs handles interruption server-side."""
        pass

    async def truncate_audio(self):
        """No-op — ElevenLabs handles audio truncation server-side."""
        pass

    async def run(self):
        """Main event processing loop for ElevenLabs WebSocket."""
        self._running = True
        try:
            async for raw_message in self.ws:
                try:
                    event = json.loads(raw_message)
                except json.JSONDecodeError:
                    logger.warning(f"Non-JSON message from ElevenLabs: {raw_message[:100]}")
                    continue

                await self._dispatch(event)
        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"ElevenLabs WebSocket closed: {e}")
        finally:
            self._running = False

    async def _dispatch(self, event: dict):
        """Route ElevenLabs events to handlers."""
        event_type = event.get("type", "")
        if event_type != "audio":
            logger.info(f"ElevenLabs event: {event_type} | keys={list(event.keys())}")

        if event_type == "conversation_initiation_metadata":
            logger.info("ElevenLabs conversation initiated")
            self._session_ready.set()

        elif event_type == "audio":
            audio_event = event.get("audio_event", {})
            audio_b64 = audio_event.get("audio_base_64", "")
            event_id = audio_event.get("event_id", 0)
            if isinstance(event_id, str):
                try:
                    event_id = int(event_id)
                except ValueError:
                    event_id = hash(event_id)
            self._current_event_id = event_id

            # Skip stale audio from before the last interruption
            if event_id <= self._last_interrupt_event_id:
                return

            if audio_b64 and self.on_audio:
                pcm_16k = base64.b64decode(audio_b64)

                # PCM16 16kHz → PCM16 8kHz
                pcm_8k, self._downsample_state = audioop.ratecv(
                    pcm_16k, 2, 1, _OUTPUT_RATE, _INPUT_RATE, self._downsample_state,
                )

                # PCM16 8kHz → μ-law
                mulaw_bytes = audioop.lin2ulaw(pcm_8k, 2)

                if not hasattr(self, '_audio_count'):
                    self._audio_count = 0
                self._audio_count += 1
                if self._audio_count == 1:
                    logger.info(f"First ElevenLabs audio: {len(mulaw_bytes)} bytes (converted)")
                elif self._audio_count % 100 == 0:
                    logger.debug(f"ElevenLabs audio chunks received: {self._audio_count}")

                await self.on_audio(mulaw_bytes)

        elif event_type == "interruption":
            int_event = event.get("interruption_event", {})
            event_id = int_event.get("event_id", 0)
            if isinstance(event_id, str):
                try:
                    event_id = int(event_id)
                except ValueError:
                    event_id = hash(event_id)
            self._last_interrupt_event_id = event_id
            logger.info(f"ElevenLabs interruption (event_id={event_id})")

            # Reset downsample state on interruption for clean audio
            self._downsample_state = None

            if self.on_speech_started:
                await self.on_speech_started()

        elif event_type == "user_transcript":
            transcript_event = event.get("user_transcription_event", {})
            text = transcript_event.get("user_transcript", "")
            if text and self.on_transcript:
                await self.on_transcript(text, "user")

        elif event_type == "agent_response":
            response_event = event.get("agent_response_event", {})
            text = response_event.get("agent_response", "")
            if text and self.on_transcript:
                await self.on_transcript(text, "assistant")
            if self.on_response_done:
                await self.on_response_done(event)

        elif event_type == "ping":
            # Must respond with pong to stay connected
            ping_event = event.get("ping_event", {})
            ping_id = ping_event.get("event_id", event.get("ping_id"))
            pong_msg = {"type": "pong", "event_id": ping_id}
            try:
                await self.ws.send(json.dumps(pong_msg))
            except Exception as e:
                logger.error(f"Error sending pong: {e}")

        elif event_type == "error":
            error = event.get("message", event)
            logger.error(f"ElevenLabs error: {error}")

        elif event_type in ("agent_response_correction", "internal_vad",
                            "user_transcript_correction"):
            logger.debug(f"ElevenLabs event: {event_type}")

        else:
            logger.debug(f"Unhandled ElevenLabs event: {event_type}")

    async def close(self):
        """Close the ElevenLabs WebSocket."""
        self._running = False
        if self.ws:
            await self.ws.close()
            logger.info("ElevenLabs WebSocket closed")
