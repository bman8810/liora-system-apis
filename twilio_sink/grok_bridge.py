"""Grok Realtime bridge for Twilio Media Streams (lab sink C).

Bidirectional μ-law (PCMU) audio via wss://api.x.ai/v1/realtime.
Docs: https://docs.x.ai/developers/model-capabilities/audio/voice
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from typing import Awaitable, Callable, Optional

import websockets

logger = logging.getLogger("twilio_sink.grok")

GROK_REALTIME_URL = os.environ.get("GROK_REALTIME_URL", "wss://api.x.ai/v1/realtime")
DEFAULT_VOICE = os.environ.get("GROK_VOICE", "Ara")
DEFAULT_INSTRUCTIONS = os.environ.get(
    "TWILIO_SINK_GROK_INSTRUCTIONS",
    (
        "You are Liora lab sink C, a short test agent on a phone call. "
        "Greet briefly, confirm you can hear the caller, answer simple questions, "
        "and keep replies under two sentences. This is a quality/recording test, not a patient."
    ),
)


class GrokRealtimeBridge:
    """Minimal Grok Realtime client for Twilio stream bridging."""

    def __init__(
        self,
        api_key: str,
        *,
        voice: str = DEFAULT_VOICE,
        instructions: str = DEFAULT_INSTRUCTIONS,
        on_audio: Optional[Callable[[bytes], Awaitable[None]]] = None,
        on_transcript: Optional[Callable[[str, str], Awaitable[None]]] = None,
        on_error: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        if not api_key:
            raise ValueError("XAI_API_KEY required for Grok Realtime")
        self.api_key = api_key
        self.voice = voice
        self.instructions = instructions
        self.on_audio = on_audio
        self.on_transcript = on_transcript
        self.on_error = on_error
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._session_ready = asyncio.Event()
        self._run_task: Optional[asyncio.Task] = None
        self._closed = False
        self.stats = {
            "audio_in_frames": 0,
            "audio_out_deltas": 0,
            "errors": 0,
            "session_ready": False,
        }

    async def connect_and_configure(self) -> None:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        logger.info("Connecting Grok Realtime %s", GROK_REALTIME_URL)
        self.ws = await websockets.connect(
            GROK_REALTIME_URL,
            additional_headers=headers,
            ping_interval=20,
            ping_timeout=20,
            max_size=10 * 1024 * 1024,
        )
        session_update = {
            "type": "session.update",
            "session": {
                "voice": self.voice,
                "instructions": self.instructions,
                "temperature": 0.8,
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.4,
                    "silence_duration_ms": 400,
                },
                "audio": {
                    "input": {"format": {"type": "audio/pcmu"}},
                    "output": {"format": {"type": "audio/pcmu"}},
                },
            },
        }
        await self.ws.send(json.dumps(session_update))
        self._run_task = asyncio.create_task(self._recv_loop(), name="grok-recv")
        try:
            await asyncio.wait_for(self._session_ready.wait(), timeout=15)
        except asyncio.TimeoutError:
            logger.warning("Grok session.updated not seen within 15s — continuing anyway")
        # Kick greeting
        await self.ws.send(json.dumps({"type": "response.create"}))

    async def send_mulaw(self, mulaw: bytes) -> None:
        if not self.ws or self._closed or not mulaw:
            return
        self.stats["audio_in_frames"] += 1
        msg = {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(mulaw).decode("ascii"),
        }
        try:
            await self.ws.send(json.dumps(msg))
        except Exception as e:
            logger.warning("Grok send_audio failed: %s", e)
            self.stats["errors"] += 1

    async def _recv_loop(self) -> None:
        assert self.ws is not None
        try:
            async for raw in self.ws:
                if self._closed:
                    break
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(event)
        except websockets.exceptions.ConnectionClosed as e:
            logger.info("Grok WS closed: %s", e)
        except Exception as e:
            logger.exception("Grok recv loop error: %s", e)
            self.stats["errors"] += 1
            if self.on_error:
                await self.on_error(str(e))

    async def _dispatch(self, event: dict) -> None:
        et = event.get("type", "")
        if et == "session.updated":
            self._session_ready.set()
            self.stats["session_ready"] = True
            logger.info("Grok session configured voice=%s", self.voice)
        elif et == "session.created":
            logger.info("Grok session created")
        elif et == "response.output_audio.delta":
            b64 = event.get("delta") or ""
            if b64 and self.on_audio:
                self.stats["audio_out_deltas"] += 1
                await self.on_audio(base64.b64decode(b64))
        elif et == "response.audio_transcript.delta":
            text = event.get("delta") or ""
            if text and self.on_transcript:
                await self.on_transcript(text, "assistant")
        elif et == "conversation.item.input_audio_transcription.completed":
            text = event.get("transcript") or ""
            if text and self.on_transcript:
                await self.on_transcript(text, "user")
        elif et == "error":
            err = event.get("error") or event
            msg = err.get("message") if isinstance(err, dict) else str(err)
            logger.error("Grok error: %s", msg)
            self.stats["errors"] += 1
            if self.on_error:
                await self.on_error(str(msg))
        elif et in (
            "response.created",
            "response.done",
            "response.output_audio.done",
            "input_audio_buffer.speech_started",
            "input_audio_buffer.speech_stopped",
            "input_audio_buffer.committed",
            "rate_limits.updated",
            "response.output_item.added",
            "response.output_item.done",
            "response.content_part.added",
            "response.content_part.done",
        ):
            logger.debug("Grok event %s", et)
        else:
            logger.debug("Grok unhandled %s", et)

    async def close(self) -> None:
        self._closed = True
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()
            try:
                await self._run_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
            self.ws = None
        logger.info("Grok bridge closed stats=%s", self.stats)
