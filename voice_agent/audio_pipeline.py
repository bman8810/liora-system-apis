"""Bidirectional audio bridge between RTP media and AI voice backend.

Inbound: RTP frame → AI backend (per-frame or chunked depending on backend)
Outbound: AI audio → slice into 160-byte frames → send immediately
Interruption: on speech_started, flush outbound + cancel AI response

Greeting policy (Barric 2026-07-23):
  - outbound: wait until callee picks up and speaks; never greet into ringback
  - inbound:  greet as soon as we answer
"""

from __future__ import annotations

import asyncio
import logging
from typing import Literal, Optional

from . import config
from .ai_bridge import AIBridge
from .media_handler import WebRTCMediaHandler

logger = logging.getLogger(__name__)

CallDirection = Literal["outbound", "inbound"]


class AudioPipeline:
    """Bridges phone RTP audio and an AI voice backend."""

    def __init__(
        self,
        media: WebRTCMediaHandler,
        bridge: AIBridge,
        *,
        direction: CallDirection = "outbound",
    ):
        self.media = media
        self.bridge = bridge
        self.direction: CallDirection = direction

        # State
        self._running = False
        self._bridge_speaking = False
        self._interrupted = False
        self._user_spoke = False
        self._greeting_sent = False
        self._greeting_task: Optional[asyncio.Task] = None
        self._inbound_count = 0
        self._outbound_count = 0

        # Wire up callbacks
        self.media.on_audio_received = self._on_rtp_audio
        self.bridge.on_audio = self._on_bridge_audio
        self.bridge.on_speech_started = self._on_speech_started
        self.bridge.on_speech_stopped = self._on_speech_stopped
        self.bridge.on_response_done = self._on_response_done
        self.bridge.on_transcript = self._on_transcript

    async def start(self):
        """Start the audio pipeline and apply direction-specific greeting policy."""
        self._running = True
        self._user_spoke = False
        self._greeting_sent = False
        await self.media.start_sending()
        logger.info("Audio pipeline started (direction=%s)", self.direction)

        if self.direction == "inbound":
            # We answered — greet immediately.
            await self._send_greeting(reason="inbound_answer")
        else:
            # Outbound: SIP/media often up before PSTN answer. Wait for speech.
            # Late fallback only if they answer and stay silent a long time.
            self._greeting_task = asyncio.create_task(self._outbound_greeting_fallback())

    async def _send_greeting(self, reason: str) -> None:
        if self._greeting_sent or not self._running:
            return
        self._greeting_sent = True
        self._cancel_greeting_task()
        logger.info("Greeting (%s) — response.create", reason)
        try:
            await self.bridge.send_response_create()
        except Exception as e:
            logger.error("Failed to send greeting: %s", e)
            self._greeting_sent = False

    async def _outbound_greeting_fallback(self):
        """Outbound only: late safety net if callee never speaks."""
        try:
            await asyncio.sleep(18.0)
            if (
                self._running
                and self.direction == "outbound"
                and not self._user_spoke
                and not self._greeting_sent
                and self._outbound_count == 0
            ):
                await self._send_greeting(reason="outbound_silent_fallback_18s")
        except asyncio.CancelledError:
            pass

    def _cancel_greeting_task(self) -> None:
        task = self._greeting_task
        if task and not task.done():
            task.cancel()
        self._greeting_task = None

    async def _on_rtp_audio(self, mulaw_bytes: bytes):
        """Inbound: RTP μ-law audio → AI backend."""
        if not self._running:
            return

        self._inbound_count += 1
        if self._inbound_count == 1:
            logger.info(
                "First inbound audio frame: %s bytes → AI backend", len(mulaw_bytes)
            )
        elif self._inbound_count % 500 == 0:
            logger.info(
                "Inbound audio frames sent to AI backend: %s", self._inbound_count
            )

        try:
            await self.bridge.send_audio(mulaw_bytes)
        except Exception as e:
            logger.error("Error sending audio to AI backend: %s", e)

    async def _on_bridge_audio(self, mulaw_bytes: bytes):
        """Outbound: AI audio → slice into frames → send immediately."""
        if not self._running or self._interrupted:
            return

        self._bridge_speaking = True
        frame_size = config.PCMU_FRAME_SIZE

        offset = 0
        while offset < len(mulaw_bytes):
            chunk = mulaw_bytes[offset : offset + frame_size]
            if len(chunk) < frame_size:
                chunk = chunk + config.PCMU_SILENCE * (frame_size - len(chunk))
            await self.media.send_audio(chunk)
            self._outbound_count += 1
            if self._outbound_count == 1:
                logger.info("First outbound frame queued to phone")
            offset += frame_size

        if self._outbound_count > 0 and self._outbound_count % 500 == 0:
            logger.info(
                "Outbound audio frames sent to phone: %s", self._outbound_count
            )

    async def _on_speech_started(self):
        """User started speaking — interrupt AI when needed."""
        first = not self._user_spoke
        self._user_spoke = True
        self._cancel_greeting_task()

        # Outbound first speech: let server_vad reply after they finish; do not
        # force greeting and do not cancel a response that hasn't started.
        if (
            first
            and self.direction == "outbound"
            and not self._greeting_sent
            and self._outbound_count == 0
        ):
            logger.info(
                "First callee speech (outbound) — wait for VAD turn; no forced greet"
            )
            return

        logger.info(
            "Interruption: user speaking, canceling AI response + flushing buffer"
        )
        self._interrupted = True
        self._bridge_speaking = False
        self.media.flush_outbound()

        try:
            await asyncio.gather(
                self.bridge.cancel_response(),
                self.bridge.truncate_audio(),
            )
        except Exception as e:
            logger.error("Error canceling AI response: %s", e)

    async def _on_speech_stopped(self):
        """User stopped speaking — allow Grok output again."""
        logger.info("User stopped speaking")
        self._interrupted = False

    async def _on_response_done(self, event: dict):
        """AI finished generating a response."""
        self._bridge_speaking = False
        self._interrupted = False
        logger.info("AI response done")

    async def _on_transcript(self, text: str, role: str):
        """Log transcripts for debugging."""
        prefix = "CALLER" if role == "user" else "LIORA"
        logger.info("[%s] %s", prefix, text)

    async def stop(self):
        """Stop the audio pipeline."""
        self._running = False
        self._cancel_greeting_task()
        logger.info("Audio pipeline stopped")
