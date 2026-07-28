"""Grok Realtime API WebSocket client.

Connects to Grok's bidirectional audio WebSocket for real-time voice conversation.
Sends/receives G.711 μ-law audio encoded as base64.
Supports custom function tools (EMA read-only scheduling).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Callable, Optional
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import websockets

from . import config
from .ai_bridge import AIBridge

logger = logging.getLogger(__name__)


def _realtime_url() -> str:
    """Ensure model=grok-voice-latest is on the WS URL."""
    base = config.GROK_REALTIME_URL or "wss://api.x.ai/v1/realtime"
    model = getattr(config, "GROK_VOICE_MODEL", None) or "grok-voice-latest"
    parts = urlparse(base)
    q = dict(parse_qsl(parts.query, keep_blank_values=True))
    q.setdefault("model", model)
    return urlunparse(parts._replace(query=urlencode(q)))


class GrokBridge(AIBridge):
    """Client for Grok's realtime voice API."""

    def __init__(
        self,
        api_key: str = "",
        on_audio: Optional[Callable] = None,
        on_speech_started: Optional[Callable] = None,
        on_speech_stopped: Optional[Callable] = None,
        on_response_done: Optional[Callable] = None,
        on_transcript: Optional[Callable] = None,
        enable_ema_tools: Optional[bool] = None,
    ):
        super().__init__(
            on_audio=on_audio,
            on_speech_started=on_speech_started,
            on_speech_stopped=on_speech_stopped,
            on_response_done=on_response_done,
            on_transcript=on_transcript,
        )
        self.api_key = api_key or config.GROK_API_KEY
        if not self.api_key:
            raise ValueError("XAI_API_KEY environment variable not set")

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._tool_flush_task: Optional[asyncio.Task] = None

        if enable_ema_tools is None:
            try:
                from .ema_tools import voice_tools_enabled
                enable_ema_tools = voice_tools_enabled()
            except Exception:
                enable_ema_tools = False
        self.enable_ema_tools = bool(enable_ema_tools)

    async def connect(self):
        """Connect to Grok realtime WebSocket."""
        self._session_ready = asyncio.Event()
        url = _realtime_url()
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

    async def configure_session(self, patient_name: str = "the patient"):
        """Send session.update to configure voice, audio format, tools, instructions."""
        if self.enable_ema_tools:
            instructions = config.SYSTEM_INSTRUCTIONS_SCHEDULING.format(
                patient_name=patient_name
            )
        else:
            instructions = config.SYSTEM_INSTRUCTIONS.format(patient_name=patient_name)

        session: dict = {
            "voice": config.GROK_VOICE,
            "instructions": instructions,
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.3,
                "silence_duration_ms": 300,
            },
            "audio": {
                "input": {"format": {"type": "audio/pcmu"}},
                "output": {"format": {"type": "audio/pcmu"}},
            },
        }

        if self.enable_ema_tools:
            from .ema_tools import EMA_TOOL_DEFINITIONS
            from .ops_tools import OPS_TOOL_DEFINITIONS
            session["tools"] = list(EMA_TOOL_DEFINITIONS) + list(OPS_TOOL_DEFINITIONS)
            logger.info(
                "EMA + ops tools enabled (%d ema, %d ops)",
                len(EMA_TOOL_DEFINITIONS),
                len(OPS_TOOL_DEFINITIONS),
            )

        session_config = {
            "type": "session.update",
            "session": session,
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

    async def _handle_function_call(self, event: dict):
        """Execute client-side function tool and return output to Grok."""
        from .ema_tools import handle_ema_tool
        from .ops_tools import handle_ops_tool, is_ops_tool

        name = event.get("name") or ""
        call_id = event.get("call_id") or ""
        raw_args = event.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
        except json.JSONDecodeError:
            arguments = {}

        logger.info("Grok function call: %s call_id=%s args_keys=%s", name, call_id, list(arguments.keys()))

        # Run blocking tool I/O off the event loop (EMA reads or ops/staff queue)
        handler = handle_ops_tool if is_ops_tool(name) else handle_ema_tool
        output = await asyncio.to_thread(handler, name, arguments)

        if not self.ws:
            return

        await self.ws.send(json.dumps({
            "type": "conversation.item.create",
            "item": {
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            },
        }))
        logger.info("Sent function_call_output for %s (%d chars)", name, len(output))

        # Debounce response.create so parallel tool calls all return first
        if self._tool_flush_task and not self._tool_flush_task.done():
            self._tool_flush_task.cancel()
            try:
                await self._tool_flush_task
            except asyncio.CancelledError:
                pass
        self._tool_flush_task = asyncio.create_task(self._flush_tool_response())

    async def _flush_tool_response(self):
        try:
            await asyncio.sleep(0.2)
            if self.ws:
                await self.ws.send(json.dumps({"type": "response.create"}))
                logger.info("Tool results flushed — response.create")
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to flush tool response.create")

    async def _dispatch(self, event: dict):
        """Route Grok events to handlers."""
        event_type = event.get("type", "")

        if event_type == "session.created":
            logger.info("Grok session created")

        elif event_type == "session.updated":
            logger.info("Grok session configured")
            self._session_ready.set()

        elif event_type == "response.function_call_arguments.done":
            await self._handle_function_call(event)

        elif event_type == "response.output_audio.delta":
            # Outbound audio from Grok
            audio_b64 = event.get("delta", "")
            if audio_b64 and self.on_audio:
                mulaw_bytes = base64.b64decode(audio_b64)
                if not hasattr(self, "_audio_delta_count"):
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
            text = event.get("delta", "")
            if text and self.on_transcript:
                await self.on_transcript(text, "assistant")

        elif event_type == "conversation.item.input_audio_transcription.completed":
            text = event.get("transcript", "")
            if text and self.on_transcript:
                await self.on_transcript(text, "user")

        elif event_type == "error":
            error = event.get("error", {})
            logger.error(f"Grok error: {error.get('message', event)}")

        elif event_type == "response.created":
            logger.info("Grok response created")

        elif event_type in (
            "response.output_item.added",
            "response.content_part.added",
            "response.content_part.done",
            "response.output_item.done",
            "rate_limits.updated",
            "input_audio_buffer.committed",
            "response.function_call_arguments.delta",
        ):
            logger.debug(f"Grok event: {event_type}")

        else:
            logger.debug(f"Unhandled Grok event: {event_type}")

    async def close(self):
        """Close the Grok WebSocket."""
        self._running = False
        if self.ws:
            await self.ws.close()
            logger.info("Grok WebSocket closed")
