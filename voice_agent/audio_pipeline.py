"""Bidirectional audio bridge between RTP media and AI voice backend.

Inbound: RTP frame → AI backend (per-frame or chunked depending on backend)
Outbound: AI audio → slice into 160-byte frames → send immediately
Interruption: speech_started is filtered by BargeInPolicy so short
backchannels and multi-intent overlap do not thrash the primary response.
"""

import asyncio
import logging
from typing import Optional

from . import config
from .ai_bridge import AIBridge
from .media_handler import WebRTCMediaHandler
from .turn_taking import BargeInAction, BargeInPolicy

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
        self._barge_policy = BargeInPolicy()
        self._soft_hold_task: Optional[asyncio.Task] = None

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

    def _sync_tool_inflight(self) -> None:
        """Reflect bridge tool/response state into the barge policy."""
        inflight = bool(getattr(self.bridge, "tool_inflight", False))
        self._barge_policy.mark_tool_inflight(inflight)
        # response.created may precede first audio delta — seed commit window.
        if bool(getattr(self.bridge, "response_active", False)):
            if not self._barge_policy.response_in_flight:
                self._barge_policy.mark_response_started()

    def _cancel_soft_hold_task(self) -> None:
        task = self._soft_hold_task
        self._soft_hold_task = None
        if task is not None and not task.done():
            # Don't cancel ourselves — _do_hard_barge may run inside this task.
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if task is not current:
                task.cancel()

    def _schedule_soft_hold(self) -> None:
        """Sleep until policy hold deadline, then poll for HARD_BARGE.

        Re-checks remaining hold time so commit-window extensions are honored
        even if the first sleep was shorter than the final deadline.
        """
        self._cancel_soft_hold_task()
        self._soft_hold_task = asyncio.create_task(self._soft_hold_wait())

    async def _soft_hold_wait(self) -> None:
        try:
            # Loop until hold expires or speech ends (task cancelled)
            while self._barge_policy.hold_pending:
                remaining = self._barge_policy.hold_remaining_s()
                if remaining is None:
                    break
                if remaining > 0:
                    await asyncio.sleep(remaining)
                self._sync_tool_inflight()
                action = self._barge_policy.poll()
                if action == BargeInAction.HARD_BARGE:
                    logger.info(
                        "Soft-hold elapsed with sustained speech — hard barge-in "
                        "(hard=%d soft=%d backchannel=%d debounced=%d)",
                        self._barge_policy.hard_barges,
                        self._barge_policy.soft_holds,
                        self._barge_policy.backchannels_ignored,
                        self._barge_policy.debounced,
                    )
                    await self._do_hard_barge()
                    return
                if action != BargeInAction.SOFT_HOLD:
                    logger.debug("Soft-hold poll result: %s", action.value)
                    return
        except asyncio.CancelledError:
            pass

    async def _do_hard_barge(self) -> None:
        """Cancel AI response + flush outbound (true interrupt)."""
        self._cancel_soft_hold_task()
        self._interrupted = True
        self._bridge_speaking = False
        # Clear assistant active so the next speech edge is not stuck in soft-hold
        # while we wait for a late response.done after cancel.
        self._barge_policy.mark_response_done()

        # Flush buffered outbound audio so the caller stops hearing AI immediately
        self.media.flush_outbound()

        try:
            await asyncio.gather(
                self.bridge.cancel_response(),
                self.bridge.truncate_audio(),
            )
        except Exception as e:
            logger.error(f"Error canceling AI response: {e}")

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
        self._barge_policy.mark_assistant_audio()
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
        """User started speaking — consult barge-in policy before canceling."""
        self._sync_tool_inflight()
        action = self._barge_policy.on_speech_started()

        if action == BargeInAction.HARD_BARGE:
            logger.info(
                "Interruption: hard barge-in (cancel AI + flush) "
                "(hard=%d soft=%d backchannel=%d debounced=%d)",
                self._barge_policy.hard_barges,
                self._barge_policy.soft_holds,
                self._barge_policy.backchannels_ignored,
                self._barge_policy.debounced,
            )
            await self._do_hard_barge()
        elif action == BargeInAction.SOFT_HOLD:
            logger.info(
                "Speech started during assistant turn — soft hold %dms "
                "(no cancel yet)",
                int(self._barge_policy.backchannel_hold_ms),
            )
            # Do not set _interrupted until hard barge
            self._schedule_soft_hold()
        else:
            logger.info(
                "Speech started ignored by barge policy "
                "(hard=%d soft=%d backchannel=%d debounced=%d)",
                self._barge_policy.hard_barges,
                self._barge_policy.soft_holds,
                self._barge_policy.backchannels_ignored,
                self._barge_policy.debounced,
            )

    async def _on_speech_stopped(self):
        """User stopped speaking — drop pending soft hold if backchannel."""
        action = self._barge_policy.on_speech_stopped()
        if action == BargeInAction.IGNORE and self._soft_hold_task is not None:
            logger.info("User stopped during soft hold — treating as backchannel")
            self._cancel_soft_hold_task()
        else:
            logger.info("User stopped speaking")

        # Only clear interrupt latch when we were not mid hard-barge recovery
        # in a way that still needs the gate; response_done also clears this.
        self._interrupted = False

    async def _on_response_done(self, event: dict):
        """AI finished generating a response."""
        self._bridge_speaking = False
        self._interrupted = False
        self._barge_policy.mark_response_done()
        logger.info("AI response done")

    async def _on_transcript(self, text: str, role: str):
        """Log transcripts for debugging."""
        prefix = "CALLER" if role == "user" else "LIORA"
        logger.info(f"[{prefix}] {text}")

    async def stop(self):
        """Stop the audio pipeline."""
        self._running = False
        self._cancel_soft_hold_task()
        logger.info("Audio pipeline stopped")
