"""Bidirectional audio bridge between RTP media and AI voice backend.

Inbound: RTP frame → AI backend (per-frame or chunked depending on backend)
Outbound: AI audio → slice into 160-byte frames → send immediately
Interruption: on speech_started, flush outbound + cancel AI response
"""

import asyncio
import logging
from typing import Optional

from . import config
from .ai_bridge import AIBridge
from .media_handler import WebRTCMediaHandler

logger = logging.getLogger(__name__)


class AudioPipeline:
    """Bridges phone RTP audio and an AI voice backend."""

    def __init__(self, media: WebRTCMediaHandler, bridge: AIBridge):
        self.media = media
        self.bridge = bridge

        # State
        self._running = False
        self._bridge_speaking = False
        self._interrupted = False

        # Wire up callbacks
        self.media.on_audio_received = self._on_rtp_audio
        self.bridge.on_audio = self._on_bridge_audio
        self.bridge.on_speech_started = self._on_speech_started
        self.bridge.on_speech_stopped = self._on_speech_stopped
        self.bridge.on_response_done = self._on_response_done
        self.bridge.on_transcript = self._on_transcript

    async def start(self):
        """Start the audio pipeline."""
        self._running = True
        await self.media.start_sending()
        logger.info("Audio pipeline started")

        # Don't trigger the greeting immediately — wait for the callee to
        # pick up and say hello.  Grok is in server_vad mode so it will hear
        # their "hello" and respond naturally.  We just need a safety net in
        # case there's silence (e.g. they pick up and wait).
        self._greeting_task = asyncio.ensure_future(self._greeting_fallback())

    async def _greeting_fallback(self):
        """If the callee doesn't speak within a few seconds, trigger the greeting."""
        try:
            await asyncio.sleep(5.0)
            if self._running and self._inbound_count > 0 and self._outbound_count == 0:
                logger.info("No speech detected from callee after 5s — triggering greeting")
                await self.bridge.send_response_create()
        except asyncio.CancelledError:
            pass

    _inbound_count = 0
    _outbound_count = 0

    async def _on_rtp_audio(self, mulaw_bytes: bytes):
        """Inbound: RTP μ-law audio → AI backend.

        Send immediately per-frame for lowest latency.
        """
        if not self._running:
            return

        self._inbound_count += 1
        if self._inbound_count == 1:
            logger.info(f"First inbound audio frame: {len(mulaw_bytes)} bytes → AI backend")
        elif self._inbound_count % 500 == 0:
            logger.info(f"Inbound audio frames sent to AI backend: {self._inbound_count}")

        try:
            await self.bridge.send_audio(mulaw_bytes)
        except Exception as e:
            logger.error(f"Error sending audio to AI backend: {e}")

    async def _on_bridge_audio(self, mulaw_bytes: bytes):
        """Outbound: AI audio → slice into frames → send immediately.

        Instead of buffering and pumping on a timer, we slice the incoming
        chunk into 160-byte frames and queue them directly to the sender track.
        This eliminates the extra 20ms+ latency from the pump loop.
        """
        if not self._running or self._interrupted:
            return

        self._bridge_speaking = True
        frame_size = config.PCMU_FRAME_SIZE

        # Slice into frames and send each immediately
        offset = 0
        while offset < len(mulaw_bytes):
            chunk = mulaw_bytes[offset:offset + frame_size]
            if len(chunk) < frame_size:
                # Pad the last chunk with silence
                chunk = chunk + config.PCMU_SILENCE * (frame_size - len(chunk))
            await self.media.send_audio(chunk)
            self._outbound_count += 1
            if self._outbound_count == 1:
                logger.info("First outbound frame queued to phone")
            offset += frame_size

        if self._outbound_count > 0 and self._outbound_count % 500 == 0:
            logger.info(f"Outbound audio frames sent to phone: {self._outbound_count}")

    async def _on_speech_started(self):
        """User started speaking — interrupt AI immediately.

        Always cancel + flush, even if _bridge_speaking is False — there may
        still be buffered audio playing on the phone from a response that
        the AI already finished generating.
        """
        logger.info("Interruption: user speaking, canceling AI response + flushing buffer")
        self._interrupted = True
        self._bridge_speaking = False

        # Flush buffered outbound audio so the caller stops hearing AI immediately
        self.media.flush_outbound()

        try:
            await asyncio.gather(
                self.bridge.cancel_response(),
                self.bridge.truncate_audio(),
            )
        except Exception as e:
            logger.error(f"Error canceling AI response: {e}")

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
        logger.info(f"[{prefix}] {text}")

    async def stop(self):
        """Stop the audio pipeline."""
        self._running = False
        logger.info("Audio pipeline stopped")
