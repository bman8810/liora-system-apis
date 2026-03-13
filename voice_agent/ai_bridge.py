"""Abstract base class for AI voice backends.

Both GrokBridge and ElevenLabsBridge implement this interface so the
audio pipeline and call manager can work with either backend.
"""

import asyncio
from abc import ABC, abstractmethod
from typing import Callable, Optional


class AIBridge(ABC):
    """Interface for real-time voice AI backends."""

    def __init__(
        self,
        on_audio: Optional[Callable] = None,
        on_speech_started: Optional[Callable] = None,
        on_speech_stopped: Optional[Callable] = None,
        on_response_done: Optional[Callable] = None,
        on_transcript: Optional[Callable] = None,
    ):
        self.on_audio = on_audio                    # async callback(mulaw_bytes)
        self.on_speech_started = on_speech_started  # async callback()
        self.on_speech_stopped = on_speech_stopped  # async callback()
        self.on_response_done = on_response_done    # async callback(dict)
        self.on_transcript = on_transcript          # async callback(str, role)

        self._session_ready: asyncio.Event = asyncio.Event()

    @abstractmethod
    async def connect(self):
        """Connect to the AI backend."""

    @abstractmethod
    async def configure_session(self, patient_name: str = "the patient"):
        """Configure the session (system prompt, voice, etc.)."""

    @abstractmethod
    async def send_audio(self, mulaw_bytes: bytes):
        """Send μ-law audio from the phone to the AI backend."""

    @abstractmethod
    async def commit_audio(self):
        """Signal end of user speech (no-op for some backends)."""

    @abstractmethod
    async def send_response_create(self):
        """Trigger the AI to generate a response (no-op for some backends)."""

    @abstractmethod
    async def cancel_response(self):
        """Cancel the current response (for interruption handling)."""

    @abstractmethod
    async def truncate_audio(self):
        """Clear/truncate the output audio buffer."""

    @abstractmethod
    async def run(self):
        """Main event processing loop."""

    @abstractmethod
    async def close(self):
        """Close the connection."""
