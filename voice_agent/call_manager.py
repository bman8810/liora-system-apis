"""Call lifecycle orchestration.

Flow: auth → SIP register → dial API → wait for INVITE → SDP negotiation
      (via aiortc WebRTC) → media setup → Grok bridge → audio pipeline →
      wait for BYE → cleanup.
"""

import asyncio
import logging
from typing import Optional

from . import config
from .auth import check_registration, fetch_sip_credentials, get_session, initiate_dial
from .audio_pipeline import AudioPipeline
from .grok_bridge import GrokBridge
from .media_handler import WebRTCMediaHandler, parse_sdp_media
from .sip_client import SipClient
from .sip_messages import SipMessage

logger = logging.getLogger(__name__)


class CallManager:
    """Orchestrates the complete call lifecycle."""

    def __init__(self, token: str, destination: str = ""):
        self.token = token
        self.destination = destination

        self.sip: Optional[SipClient] = None
        self.media: Optional[WebRTCMediaHandler] = None
        self.grok: Optional[GrokBridge] = None
        self.pipeline: Optional[AudioPipeline] = None

        self._call_ended: Optional[asyncio.Event] = None
        self._invite_received: Optional[asyncio.Event] = None
        self._pending_invite: Optional[SipMessage] = None

    async def run(self):
        """Execute the full call flow."""
        # Create events inside the running loop (Python 3.9 compat)
        self._call_ended = asyncio.Event()
        self._invite_received = asyncio.Event()

        try:
            # Phase 1: Auth + fetch SIP credentials
            logger.info("=== Phase 1: Authentication ===")
            session = get_session(self.token)
            creds = fetch_sip_credentials(session)
            logger.info(
                f"SIP credentials: {creds['username']}@{creds['domain']} "
                f"(ext {creds['extension']})"
            )

            # Phase 2: SIP registration
            logger.info("=== Phase 2: SIP Registration ===")
            self.sip = SipClient(
                username=creds["username"],
                password=creds["password"],
                domain=creds["domain"],
                proxy=creds["proxy"],
                on_invite=self._on_invite,
                on_bye=self._on_bye,
            )

            await self.sip.connect()

            # Start SIP message loop in background
            sip_task = asyncio.create_task(self.sip.run())

            # Register
            registered = await self.sip.register()
            if not registered:
                logger.error("SIP registration failed")
                return

            # Verify registration via API
            try:
                reg_status = check_registration(session)
                logger.info(f"Registration status: {reg_status}")
            except Exception as e:
                logger.warning(f"Could not verify registration via API: {e}")

            # Phase 3: Initiate outbound call
            if self.destination:
                logger.info(f"=== Phase 3: Dialing {self.destination} ===")
                result = initiate_dial(session, self.destination)
                logger.info(f"Dial result: {result}")
            else:
                logger.info("=== Phase 3: Waiting for incoming call ===")

            # Phase 4: Wait for INVITE
            logger.info("Waiting for incoming INVITE...")
            try:
                await asyncio.wait_for(self._invite_received.wait(), timeout=60)
            except asyncio.TimeoutError:
                logger.error("Timed out waiting for INVITE (60s)")
                return

            invite = self._pending_invite
            logger.info(f"Got INVITE — Call-ID: {invite.call_id}")

            # Phase 5: Set up media via aiortc WebRTC
            logger.info("=== Phase 5: Media Setup (WebRTC/aiortc) ===")
            self.media = WebRTCMediaHandler()

            sdp_offer = invite.body
            if not sdp_offer:
                logger.error("INVITE has no SDP body — cannot proceed")
                return

            # Log offer details
            media_info = parse_sdp_media(sdp_offer)
            logger.info(
                f"SDP offer: {media_info['ip']}:{media_info['port']} "
                f"transport={media_info['transport']} "
                f"codecs={media_info['codecs']} "
                f"ICE={media_info['requires_ice']} "
                f"SRTP={media_info['requires_srtp']}"
            )

            # Let aiortc handle ICE + DTLS + SRTP
            sdp_answer = await self.media.handle_offer(sdp_offer)

            # Send 200 OK with aiortc's SDP answer
            await self.sip.send_200_ok(invite, sdp_body=sdp_answer)
            logger.info("Sent 200 OK with WebRTC SDP answer")

            # Phase 6: Connect to Grok
            logger.info("=== Phase 6: Grok Realtime ===")
            self.grok = GrokBridge()
            await self.grok.connect()
            await self.grok.configure_session()

            # Start Grok message loop in background
            grok_task = asyncio.create_task(self.grok.run())

            # Wait for session to be configured
            try:
                await asyncio.wait_for(self.grok._session_ready.wait(), timeout=10)
            except asyncio.TimeoutError:
                logger.warning("Grok session.updated not received in 10s, proceeding anyway")

            # Phase 7: Start audio pipeline
            logger.info("=== Phase 7: Audio Pipeline ===")
            logger.info("Grok session ready + media established — starting audio pipeline")
            self.pipeline = AudioPipeline(self.media, self.grok)
            await self.pipeline.start()
            logger.info("Audio pipeline active — call is live!")

            # Wait for call to end
            logger.info("=== Call Active — waiting for BYE ===")
            await self._call_ended.wait()
            logger.info("Call ended")

            # Cleanup
            await self._cleanup(sip_task, grok_task)

        except Exception as e:
            logger.error(f"Call failed: {e}", exc_info=True)
            await self._cleanup()

    async def _on_invite(self, invite: SipMessage):
        """Handle incoming INVITE from SIP."""
        logger.info(f"INVITE received: {invite.from_header}")
        self._pending_invite = invite
        self._invite_received.set()

    async def _on_bye(self, bye: SipMessage):
        """Handle BYE from SIP."""
        logger.info("BYE received — ending call")
        self._call_ended.set()

    async def _cleanup(self, *tasks):
        """Clean up all resources."""
        logger.info("Cleaning up...")

        if self.pipeline:
            await self.pipeline.stop()

        if self.grok:
            await self.grok.close()

        if self.media:
            await self.media.close()

        if self.sip:
            try:
                await self.sip.send_bye()
            except Exception:
                pass
            await self.sip.close()

        for task in tasks:
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        logger.info("Cleanup complete")
