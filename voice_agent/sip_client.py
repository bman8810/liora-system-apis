"""SIP over WebSocket User Agent.

Connects to Weave's SIP WebSocket proxy, handles REGISTER with digest auth,
listens for incoming INVITEs, and manages call signaling.
"""

import asyncio
import logging
from typing import Callable, Optional

import websockets

from . import config
from .sip_messages import (
    SipMessage,
    SipMessageBuilder,
    build_authorization_header,
    compute_digest_response,
    parse_sip_message,
    parse_www_authenticate,
)

logger = logging.getLogger(__name__)


class SipClient:
    """WebSocket-based SIP UA for Weave's phone system."""

    def __init__(
        self,
        username: str,
        password: str,
        domain: str,
        proxy: str,
        on_invite: Optional[Callable] = None,
        on_bye: Optional[Callable] = None,
    ):
        self.username = username
        self.password = password
        self.domain = domain
        self.proxy = proxy
        self.on_invite = on_invite  # async callback(SipMessage)
        self.on_bye = on_bye        # async callback(SipMessage)

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.builder = SipMessageBuilder(username, domain, proxy)
        self.registered = False
        self._running = False
        self._register_event: Optional[asyncio.Event] = None
        self._active_call: Optional[SipMessage] = None  # current INVITE
        self._re_register_task: Optional[asyncio.Task] = None

    async def connect(self):
        """Connect to the SIP WebSocket proxy."""
        self._register_event = asyncio.Event()
        url = f"wss://{self.proxy}"
        logger.info(f"Connecting to {url}...")

        self.ws = await websockets.connect(
            url,
            subprotocols=[config.SIP_WS_SUBPROTOCOL],
            additional_headers={
                "Sec-WebSocket-Protocol": config.SIP_WS_SUBPROTOCOL,
            },
            ping_interval=30,
            ping_timeout=10,
        )
        logger.info("WebSocket connected")

    async def register(self, timeout: float = 15.0) -> bool:
        """Perform SIP REGISTER with digest auth challenge/response.

        Returns True on successful registration.
        """
        # Step 1: Send initial REGISTER (will get 401)
        self._register_event.clear()
        reg = self.builder.build_register()
        logger.debug(f"Sending REGISTER (initial):\n{reg}")
        await self.ws.send(reg)

        # Wait for 401 or 200
        try:
            registered = await asyncio.wait_for(
                self._register_event.wait(), timeout=timeout
            )
        except asyncio.TimeoutError:
            logger.error("REGISTER timed out")
            return False

        return self.registered

    async def _handle_register_response(self, msg: SipMessage):
        """Handle 401 challenge or 200 OK for REGISTER."""
        if msg.status_code == 401:
            # Extract WWW-Authenticate challenge
            www_auth = msg.get_header("WWW-Authenticate")
            if not www_auth:
                logger.error("401 without WWW-Authenticate header")
                self._register_event.set()
                return

            challenge = parse_www_authenticate(www_auth)
            logger.info(f"Got 401 challenge: realm={challenge.get('realm')}")

            # Compute digest response
            digest = compute_digest_response(
                username=self.username,
                password=self.password,
                realm=challenge["realm"],
                nonce=challenge["nonce"],
                method="REGISTER",
                uri=f"sip:{self.domain}",
                qop=challenge.get("qop", ""),
            )
            auth_header = build_authorization_header(digest)

            # Send REGISTER with Authorization
            reg = self.builder.build_register(authorization=auth_header)
            logger.debug(f"Sending REGISTER (with auth):\n{reg}")
            await self.ws.send(reg)

        elif msg.status_code == 200:
            self.registered = True
            logger.info("SIP REGISTER successful — 200 OK")
            self._register_event.set()

        else:
            logger.error(f"REGISTER failed: {msg.status_code} {msg.reason}")
            self._register_event.set()

    async def send_200_ok(self, invite: SipMessage, sdp_body: str = ""):
        """Send 200 OK response to an INVITE with SDP answer."""
        extra_headers = {}
        if sdp_body:
            extra_headers["Content-Type"] = "application/sdp"

        response = self.builder.build_response(
            invite,
            status_code=200,
            reason="OK",
            body=sdp_body,
            extra_headers=extra_headers if not sdp_body else None,
        )
        logger.debug(f"Sending 200 OK:\n{response}")
        await self.ws.send(response)

    async def send_bye(self):
        """Send BYE to end the current call."""
        if not self._active_call:
            logger.warning("No active call to BYE")
            return

        invite = self._active_call
        bye = self.builder.build_bye(
            call_id=invite.call_id,
            from_header=invite.to_header,  # swap From/To for our direction
            to_header=invite.from_header,
        )
        logger.debug(f"Sending BYE:\n{bye}")
        await self.ws.send(bye)
        self._active_call = None

    async def run(self):
        """Main message processing loop."""
        self._running = True

        # Start periodic re-registration
        self._re_register_task = asyncio.create_task(self._re_register_loop())

        try:
            async for raw_message in self.ws:
                if isinstance(raw_message, bytes):
                    raw_message = raw_message.decode("utf-8", errors="replace")

                logger.debug(f"Received SIP:\n{raw_message}")
                msg = parse_sip_message(raw_message)

                await self._dispatch(msg)
        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"WebSocket closed: {e}")
        finally:
            self._running = False
            if self._re_register_task:
                self._re_register_task.cancel()

    async def _dispatch(self, msg: SipMessage):
        """Route a received SIP message to the appropriate handler."""
        if not msg.is_request:
            # Response — check CSeq method
            cseq = msg.cseq
            if "REGISTER" in cseq:
                await self._handle_register_response(msg)
            elif "BYE" in cseq:
                logger.info(f"BYE response: {msg.status_code}")
            elif "INVITE" in cseq:
                logger.info(f"INVITE response: {msg.status_code}")
            return

        # Request
        method = msg.method
        if method == "INVITE":
            logger.info(f"Incoming INVITE from {msg.from_header}")
            self._active_call = msg

            if self.on_invite:
                await self.on_invite(msg)
            else:
                # Auto-answer with empty 200 OK
                await self.send_200_ok(msg)

        elif method == "ACK":
            logger.info("Received ACK — call established")

        elif method == "BYE":
            logger.info(f"Received BYE — call ended")
            self._active_call = None

            # Send 200 OK to BYE
            response = self.builder.build_response(msg, 200, "OK")
            await self.ws.send(response)

            if self.on_bye:
                await self.on_bye(msg)

        elif method == "OPTIONS":
            # Keepalive/ping — respond 200 OK
            response = self.builder.build_response(msg, 200, "OK")
            await self.ws.send(response)

        elif method == "NOTIFY":
            # Accept notifications
            response = self.builder.build_response(msg, 200, "OK")
            await self.ws.send(response)

        elif method == "CANCEL":
            logger.info("Received CANCEL")
            response = self.builder.build_response(msg, 200, "OK")
            await self.ws.send(response)
            self._active_call = None

        else:
            logger.warning(f"Unhandled SIP method: {method}")

    async def _re_register_loop(self):
        """Periodically re-register to keep SIP registration alive."""
        while self._running:
            await asyncio.sleep(300)  # Re-register every 5 minutes
            if self.registered and self.ws:
                logger.info("Sending periodic re-REGISTER")
                try:
                    reg = self.builder.build_register()
                    await self.ws.send(reg)
                except Exception as e:
                    logger.error(f"Re-REGISTER failed: {e}")

    async def close(self):
        """Close the SIP connection."""
        self._running = False
        if self._re_register_task:
            self._re_register_task.cancel()
        if self.ws:
            await self.ws.close()
            logger.info("SIP WebSocket closed")
