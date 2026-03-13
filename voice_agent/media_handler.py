"""WebRTC media endpoint using aiortc for DTLS-SRTP + ICE.

Handles the SDP offer from FreeSWITCH (Weave's media server), completes
ICE + DTLS negotiation, and provides raw G.711 μ-law audio frames.
"""

import asyncio
import audioop
import fractions
import logging
import time
from typing import Callable, Optional

from aiortc import (
    RTCPeerConnection,
    RTCSessionDescription,
    MediaStreamTrack,
)
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame

from . import config

logger = logging.getLogger(__name__)


def mulaw_to_pcm16(mulaw_bytes: bytes) -> bytes:
    """Convert μ-law bytes to 16-bit linear PCM (little-endian)."""
    return audioop.ulaw2lin(mulaw_bytes, 2)


def pcm16_to_mulaw(pcm_bytes: bytes) -> bytes:
    """Convert 16-bit linear PCM (little-endian) to μ-law bytes."""
    return audioop.lin2ulaw(pcm_bytes, 2)


def parse_sdp_media(sdp: str) -> dict:
    """Extract media connection info from SDP."""
    result = {
        "ip": "0.0.0.0",
        "port": 0,
        "transport": "RTP/AVP",
        "codecs": [],
        "ice_ufrag": "",
        "ice_pwd": "",
        "fingerprint": "",
        "candidates": [],
        "requires_srtp": False,
        "requires_ice": False,
    }

    for line in sdp.replace("\r\n", "\n").split("\n"):
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            result["ip"] = line.split()[-1]
        elif line.startswith("m=audio "):
            parts = line.split()
            result["port"] = int(parts[1])
            result["transport"] = parts[2]
            result["codecs"] = [int(p) for p in parts[3:] if p.isdigit()]
            if "SAVP" in result["transport"]:
                result["requires_srtp"] = True
        elif line.startswith("a=ice-ufrag:"):
            result["ice_ufrag"] = line.split(":", 1)[1]
            result["requires_ice"] = True
        elif line.startswith("a=ice-pwd:"):
            result["ice_pwd"] = line.split(":", 1)[1]
        elif line.startswith("a=fingerprint:"):
            result["fingerprint"] = line.split(":", 1)[1].strip()
            result["requires_srtp"] = True
        elif line.startswith("a=candidate:"):
            result["candidates"].append(line)

    return result


class MulawSenderTrack(MediaStreamTrack):
    """MediaStreamTrack that sends μ-law audio frames from a queue.

    aiortc expects AudioFrame objects with s16 PCM. We convert μ-law → PCM16
    on the fly. aiortc then re-encodes to PCMU for the wire (since we negotiate
    PCMU in the SDP).
    """
    kind = "audio"

    def __init__(self):
        super().__init__()
        self._queue: asyncio.Queue = asyncio.Queue()  # unbounded — Grok sends faster than real-time
        self._timestamp = 0
        self._start_time = None
        self._silence_pcm = mulaw_to_pcm16(config.PCMU_SILENCE * config.PCMU_FRAME_SIZE)

    async def recv(self) -> AudioFrame:
        """Called by aiortc to get the next audio frame to send.

        Must self-pace at 20ms intervals — aiortc's sender loop calls recv()
        in a tight loop and relies on us to control frame timing.
        """
        if self._start_time is None:
            self._start_time = time.monotonic()

        # Pace: wait until wall-clock catches up to this frame's target time
        target_time = self._start_time + (self._timestamp / config.PCMU_SAMPLE_RATE)
        wait = target_time - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

        # Get pre-converted PCM16 data from queue, or generate silence
        try:
            pcm_bytes = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            pcm_bytes = self._silence_pcm

        if not hasattr(self, '_recv_count'):
            self._recv_count = 0
            self._queued_count = 0
        self._recv_count += 1
        if pcm_bytes is not self._silence_pcm:
            self._queued_count += 1
            if self._queued_count == 1:
                logger.info("Sender track: first real audio frame dequeued for RTP")
        if self._recv_count == 1:
            logger.info("Sender track: recv() called — RTP sender active")
        elif self._recv_count == 50:
            logger.info(f"Sender track: 50 frames sent (1s), {self._queued_count} had audio")

        # Create AudioFrame
        frame = AudioFrame(format="s16", layout="mono", samples=config.PCMU_FRAME_SIZE)
        frame.planes[0].update(pcm_bytes)
        frame.sample_rate = config.PCMU_SAMPLE_RATE
        frame.pts = self._timestamp
        frame.time_base = fractions.Fraction(1, config.PCMU_SAMPLE_RATE)
        self._timestamp += config.PCMU_FRAME_SIZE

        return frame

    async def put_audio(self, mulaw_bytes: bytes):
        """Queue μ-law audio for sending (converts to PCM16 upfront)."""
        # Convert to PCM16 here so recv() is fast
        pcm_bytes = mulaw_to_pcm16(mulaw_bytes)
        self._queue.put_nowait(pcm_bytes)


class WebRTCMediaHandler:
    """WebRTC media handler using aiortc (Plan B: DTLS-SRTP + ICE)."""

    def __init__(
        self,
        on_audio_received: Optional[Callable] = None,
    ):
        self.on_audio_received = on_audio_received  # async callback(mulaw_bytes)
        self.pc: Optional[RTCPeerConnection] = None
        self._sender_track: Optional[MulawSenderTrack] = None
        self._receiver_task: Optional[asyncio.Task] = None
        self._running = False

    async def handle_offer(self, sdp_offer: str) -> str:
        """Process SDP offer and return SDP answer.

        Creates RTCPeerConnection, sets remote description, creates answer.
        """
        self.pc = RTCPeerConnection()
        self._sender_track = MulawSenderTrack()

        # Add our outbound audio track
        self.pc.addTrack(self._sender_track)

        # Handle incoming audio track
        @self.pc.on("track")
        def on_track(track):
            logger.info(f"Got remote track: {track.kind}")
            if track.kind == "audio":
                self._receiver_task = asyncio.ensure_future(
                    self._receive_audio(track)
                )

        @self.pc.on("connectionstatechange")
        async def on_connection_state():
            logger.info(f"WebRTC connection state: {self.pc.connectionState}")
            if self.pc.connectionState == "failed":
                logger.error("WebRTC connection failed!")
            elif self.pc.connectionState == "connected":
                logger.info("WebRTC media path established!")

        @self.pc.on("iceconnectionstatechange")
        async def on_ice_state():
            logger.info(f"ICE connection state: {self.pc.iceConnectionState}")

        # Mark as running before SDP processing (track callback fires during setRemoteDescription)
        self._running = True

        # Fix SDP for aiortc compatibility
        sdp_offer = self._fix_sdp(sdp_offer)

        # Set remote offer
        offer = RTCSessionDescription(sdp=sdp_offer, type="offer")
        await self.pc.setRemoteDescription(offer)

        # Create and set local answer
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)

        logger.info(f"Generated SDP answer ({len(self.pc.localDescription.sdp)} bytes)")
        return self.pc.localDescription.sdp

    @staticmethod
    def _fix_sdp(sdp: str) -> str:
        """Fix SDP quirks from FreeSWITCH for aiortc compatibility.

        - a=rtcp:PORT  →  a=rtcp:PORT IN IP4 <connection-ip>
          (aiortc expects the full form with address)
        """
        # Extract connection IP from c= line
        conn_ip = "0.0.0.0"
        for line in sdp.split("\n"):
            line = line.strip().rstrip("\r")
            if line.startswith("c=IN IP4 "):
                conn_ip = line.split()[-1]
                break

        fixed_lines = []
        for line in sdp.split("\n"):
            stripped = line.rstrip("\r")
            # Fix bare a=rtcp:PORT (no address) → add IN IP4 <ip>
            if stripped.strip().startswith("a=rtcp:") and " IN " not in stripped:
                port = stripped.strip().split(":")[1].strip()
                fixed_lines.append(f"a=rtcp:{port} IN IP4 {conn_ip}")
            else:
                fixed_lines.append(stripped)

        return "\n".join(fixed_lines)

    async def _receive_audio(self, track: MediaStreamTrack):
        """Receive audio frames from the remote peer, convert to μ-law."""
        logger.info("Audio receiver started, waiting for frames...")
        frame_count = 0
        while True:
            try:
                frame = await track.recv()
                frame_count += 1

                if frame_count == 1:
                    logger.info(f"First audio frame received! format={frame.format.name} samples={frame.samples} rate={frame.sample_rate}")
                elif frame_count % 500 == 0:
                    logger.info(f"Audio frames received: {frame_count}")

                # Convert PCM16 frame to μ-law
                pcm_bytes = bytes(frame.planes[0])
                mulaw_bytes = pcm16_to_mulaw(pcm_bytes)

                if self.on_audio_received:
                    await self.on_audio_received(mulaw_bytes)

            except MediaStreamError:
                logger.info(f"Remote audio track ended after {frame_count} frames")
                break
            except Exception as e:
                logger.error(f"Audio receive error: {e}")
                break

    async def send_audio(self, mulaw_bytes: bytes):
        """Queue μ-law audio for sending via WebRTC."""
        if self._sender_track:
            await self._sender_track.put_audio(mulaw_bytes)

    def flush_outbound(self):
        """Drain the outbound queue immediately (for interruption handling)."""
        if self._sender_track:
            count = 0
            while not self._sender_track._queue.empty():
                try:
                    self._sender_track._queue.get_nowait()
                    count += 1
                except asyncio.QueueEmpty:
                    break
            if count:
                logger.info(f"Flushed {count} buffered outbound frames")

    async def start_sending(self):
        """No-op for WebRTC — sending starts automatically via track.recv()."""
        self._running = True
        logger.info("WebRTC media handler ready for audio")

    async def close(self):
        """Close the WebRTC connection."""
        self._running = False
        if self._receiver_task:
            self._receiver_task.cancel()
        if self.pc:
            await self.pc.close()
        logger.info("WebRTC media handler closed")
