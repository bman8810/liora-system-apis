"""Minimal SIP message parser/builder for WebSocket SIP UA.

Handles REGISTER, INVITE responses, ACK, BYE, and SIP digest authentication.
"""

import hashlib
import random
import string
import time
from dataclasses import dataclass, field


def _generate_tag() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=10))


def _generate_branch() -> str:
    return "z9hG4bK" + "".join(random.choices(string.ascii_lowercase + string.digits, k=16))


def _generate_call_id() -> str:
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=24))


@dataclass
class SipMessage:
    """Parsed SIP message."""
    is_request: bool = True
    method: str = ""           # REGISTER, INVITE, ACK, BYE, etc.
    request_uri: str = ""      # For requests
    status_code: int = 0       # For responses
    reason: str = ""           # For responses
    headers: dict = field(default_factory=dict)
    # Multi-value headers stored as lists (Via, Record-Route, etc.)
    header_lists: dict = field(default_factory=dict)
    body: str = ""
    raw: str = ""

    @property
    def call_id(self) -> str:
        return self.headers.get("Call-ID", self.headers.get("call-id", ""))

    @property
    def cseq(self) -> str:
        return self.headers.get("CSeq", self.headers.get("cseq", ""))

    @property
    def from_header(self) -> str:
        return self.headers.get("From", self.headers.get("from", ""))

    @property
    def to_header(self) -> str:
        return self.headers.get("To", self.headers.get("to", ""))

    @property
    def via(self) -> str:
        return self.headers.get("Via", self.headers.get("via", ""))

    @property
    def contact(self) -> str:
        return self.headers.get("Contact", self.headers.get("contact", ""))

    @property
    def content_type(self) -> str:
        return self.headers.get("Content-Type", self.headers.get("content-type", ""))

    def get_header(self, name: str) -> str:
        """Case-insensitive header lookup."""
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return ""


# Compact SIP header name mappings (RFC 3261 §7.3.3)
_COMPACT_HEADERS = {
    "v": "Via",
    "f": "From",
    "t": "To",
    "i": "Call-ID",
    "m": "Contact",
    "l": "Content-Length",
    "c": "Content-Type",
    "k": "Supported",
    "u": "Allow-Events",
    "x": "Session-Expires",
}


def parse_sip_message(text: str) -> SipMessage:
    """Parse a SIP message from raw text."""
    msg = SipMessage(raw=text)

    # Split headers and body
    if "\r\n\r\n" in text:
        header_section, msg.body = text.split("\r\n\r\n", 1)
    elif "\n\n" in text:
        header_section, msg.body = text.split("\n\n", 1)
    else:
        header_section = text
        msg.body = ""

    lines = header_section.replace("\r\n", "\n").split("\n")
    if not lines:
        return msg

    # Parse start line
    start_line = lines[0]
    if start_line.startswith("SIP/2.0"):
        # Response: SIP/2.0 200 OK
        msg.is_request = False
        parts = start_line.split(" ", 2)
        msg.status_code = int(parts[1])
        msg.reason = parts[2] if len(parts) > 2 else ""
    else:
        # Request: REGISTER sip:domain SIP/2.0
        msg.is_request = True
        parts = start_line.split(" ", 2)
        msg.method = parts[0]
        msg.request_uri = parts[1] if len(parts) > 1 else ""

    # Parse headers (preserving multi-value headers like Via, Record-Route)
    for line in lines[1:]:
        if not line:
            continue
        if ":" in line:
            name, value = line.split(":", 1)
            name = name.strip()
            value = value.strip()

            # Expand compact header names
            if name in _COMPACT_HEADERS:
                name = _COMPACT_HEADERS[name]

            # Store first occurrence in headers dict
            if name not in msg.headers:
                msg.headers[name] = value

            # Always append to header_lists for multi-value support
            if name not in msg.header_lists:
                msg.header_lists[name] = []
            msg.header_lists[name].append(value)

    return msg


def parse_www_authenticate(header: str) -> dict:
    """Parse WWW-Authenticate header into dict of realm, nonce, etc."""
    result = {}
    # Remove "Digest " prefix
    if header.lower().startswith("digest "):
        header = header[7:]

    # Parse key="value" pairs
    for part in _split_auth_params(header):
        part = part.strip()
        if "=" in part:
            key, value = part.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"')
            result[key] = value
    return result


def _split_auth_params(s: str) -> list:
    """Split comma-separated auth params, respecting quoted strings."""
    parts = []
    current = ""
    in_quotes = False
    for c in s:
        if c == '"':
            in_quotes = not in_quotes
            current += c
        elif c == ',' and not in_quotes:
            parts.append(current)
            current = ""
        else:
            current += c
    if current:
        parts.append(current)
    return parts


def compute_digest_response(
    username: str,
    password: str,
    realm: str,
    nonce: str,
    method: str,
    uri: str,
    qop: str = "",
    nc: str = "00000001",
    cnonce: str = "",
) -> dict:
    """Compute SIP digest authentication response per RFC 2617.

    Returns dict with all fields needed for Authorization header.
    """
    if not cnonce:
        cnonce = "".join(random.choices(string.ascii_lowercase + string.digits, k=16))

    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()

    if qop == "auth":
        response = hashlib.md5(
            f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()
        ).hexdigest()
    else:
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()

    result = {
        "username": username,
        "realm": realm,
        "nonce": nonce,
        "uri": uri,
        "response": response,
        "algorithm": "MD5",
    }
    if qop:
        result["qop"] = qop
        result["nc"] = nc
        result["cnonce"] = cnonce
    return result


def build_authorization_header(digest: dict) -> str:
    """Build Authorization header value from digest dict."""
    parts = [f'Digest username="{digest["username"]}"']
    parts.append(f'realm="{digest["realm"]}"')
    parts.append(f'nonce="{digest["nonce"]}"')
    parts.append(f'uri="{digest["uri"]}"')
    parts.append(f'response="{digest["response"]}"')
    parts.append(f'algorithm={digest["algorithm"]}')
    if "qop" in digest:
        parts.append(f'qop={digest["qop"]}')
        parts.append(f'nc={digest["nc"]}')
        parts.append(f'cnonce="{digest["cnonce"]}"')
    return ", ".join(parts)


class SipMessageBuilder:
    """Builds SIP messages for the WebSocket UA."""

    def __init__(self, username: str, domain: str, proxy: str):
        self.username = username
        self.domain = domain
        self.proxy = proxy
        self.local_tag = _generate_tag()
        self.call_id_register = _generate_call_id()
        self.cseq_counter = 0
        # For WebSocket transport, Contact uses ws instance id
        self.instance_id = "".join(random.choices(string.hexdigits[:16], k=16))

    def _next_cseq(self) -> int:
        self.cseq_counter += 1
        return self.cseq_counter

    @property
    def aor(self) -> str:
        """Address of Record."""
        return f"sip:{self.username}@{self.domain}"

    @property
    def contact_uri(self) -> str:
        """Contact URI for WebSocket transport."""
        return f"sip:{self.username}@{self.domain};transport=ws"

    def build_register(self, expires: int = 600, authorization: str = "") -> str:
        """Build a REGISTER request."""
        branch = _generate_branch()
        cseq = self._next_cseq()

        lines = [
            f"REGISTER sip:{self.domain} SIP/2.0",
            f"Via: SIP/2.0/WSS {self.proxy};branch={branch}",
            f"Max-Forwards: 70",
            f"From: <{self.aor}>;tag={self.local_tag}",
            f"To: <{self.aor}>",
            f"Call-ID: {self.call_id_register}",
            f"CSeq: {cseq} REGISTER",
            f"Contact: <{self.contact_uri}>;expires={expires}"
            f';+sip.instance="<urn:uuid:{self.instance_id}>"',
            f"Expires: {expires}",
            f"Allow: INVITE,ACK,CANCEL,BYE,NOTIFY,REFER,MESSAGE,OPTIONS,INFO,SUBSCRIBE",
            f"Supported: path,gruu,outbound",
            f"User-Agent: LioraVoiceAgent/1.0",
        ]

        if authorization:
            lines.append(f"Authorization: {authorization}")

        lines.append("Content-Length: 0")
        lines.append("")
        lines.append("")

        return "\r\n".join(lines)

    def build_response(
        self,
        request: SipMessage,
        status_code: int,
        reason: str = "OK",
        body: str = "",
        extra_headers: dict = None,
    ) -> str:
        """Build a SIP response to a received request (e.g., 200 OK to INVITE)."""
        lines = [f"SIP/2.0 {status_code} {reason}"]

        # Copy ALL Via headers from request (order matters)
        via_list = request.header_lists.get("Via", [])
        for via in via_list:
            lines.append(f"Via: {via}")

        # Copy Record-Route headers (required for proper routing)
        rr_list = request.header_lists.get("Record-Route", [])
        for rr in rr_list:
            lines.append(f"Record-Route: {rr}")

        # Copy From, To, Call-ID, CSeq
        lines.append(f"From: {request.from_header}")

        to_hdr = request.to_header
        if ";tag=" not in to_hdr:
            to_hdr += f";tag={_generate_tag()}"
        lines.append(f"To: {to_hdr}")

        lines.append(f"Call-ID: {request.call_id}")
        lines.append(f"CSeq: {request.cseq}")
        lines.append(f"Contact: <{self.contact_uri}>")
        lines.append(f"Allow: INVITE,ACK,CANCEL,BYE,NOTIFY,REFER,MESSAGE,OPTIONS,INFO,SUBSCRIBE")
        lines.append(f"User-Agent: LioraVoiceAgent/1.0")

        if extra_headers:
            for k, v in extra_headers.items():
                lines.append(f"{k}: {v}")

        content_length = len(body.encode()) if body else 0
        if body:
            lines.append(f"Content-Type: application/sdp")
        lines.append(f"Content-Length: {content_length}")
        lines.append("")
        if body:
            lines.append(body)
        else:
            lines.append("")

        return "\r\n".join(lines)

    def build_ack(self, invite: SipMessage) -> str:
        """Build ACK for a received INVITE response or incoming INVITE."""
        branch = _generate_branch()

        # For ACK to incoming INVITE, use the Request-URI from the INVITE
        request_uri = invite.request_uri if invite.is_request else self.aor

        lines = [
            f"ACK {request_uri} SIP/2.0",
            f"Via: SIP/2.0/WSS {self.proxy};branch={branch}",
            f"Max-Forwards: 70",
            f"From: {invite.to_header}",
            f"To: {invite.from_header}",
            f"Call-ID: {invite.call_id}",
            f"CSeq: {invite.cseq.split()[0]} ACK",
            f"User-Agent: LioraVoiceAgent/1.0",
            f"Content-Length: 0",
            "",
            "",
        ]
        return "\r\n".join(lines)

    def build_bye(self, call_id: str, from_header: str, to_header: str, cseq_num: int = None) -> str:
        """Build a BYE request to end a call."""
        branch = _generate_branch()
        cseq = cseq_num or self._next_cseq()

        # Extract URI from To header for Request-URI
        to_uri = to_header
        if "<" in to_uri:
            to_uri = to_uri.split("<")[1].split(">")[0]

        lines = [
            f"BYE {to_uri} SIP/2.0",
            f"Via: SIP/2.0/WSS {self.proxy};branch={branch}",
            f"Max-Forwards: 70",
            f"From: {from_header}",
            f"To: {to_header}",
            f"Call-ID: {call_id}",
            f"CSeq: {cseq} BYE",
            f"User-Agent: LioraVoiceAgent/1.0",
            f"Content-Length: 0",
            "",
            "",
        ]
        return "\r\n".join(lines)

    def build_sdp_answer(
        self,
        local_ip: str,
        local_rtp_port: int,
        offer_sdp: str,
    ) -> str:
        """Build SDP answer for incoming INVITE.

        Offers PCMU (payload type 0) only. Uses RTP/AVP (plain RTP) by default.
        If the offer uses RTP/SAVP or RTP/SAVPF, we'll need to handle SRTP.
        """
        # Parse offer to determine transport
        transport = "RTP/AVP"
        for line in offer_sdp.split("\r\n"):
            if line.startswith("m=audio"):
                parts = line.split()
                if len(parts) >= 3:
                    transport = parts[2]
                break

        sdp_lines = [
            "v=0",
            f"o=liora {int(time.time())} {int(time.time())} IN IP4 {local_ip}",
            "s=LioraVoiceAgent",
            f"c=IN IP4 {local_ip}",
            "t=0 0",
            f"m=audio {local_rtp_port} {transport} 0",
            "a=rtpmap:0 PCMU/8000",
            "a=ptime:20",
            "a=sendrecv",
        ]

        # If offer has ICE candidates, we need to handle ICE
        if "a=ice-ufrag:" in offer_sdp:
            ice_ufrag = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
            ice_pwd = "".join(random.choices(string.ascii_lowercase + string.digits, k=24))
            sdp_lines.append(f"a=ice-ufrag:{ice_ufrag}")
            sdp_lines.append(f"a=ice-pwd:{ice_pwd}")

        # If offer has DTLS fingerprint, we need SRTP
        if "a=fingerprint:" in offer_sdp:
            # This indicates SRTP is required — flag for fallback to aiortc
            pass

        return "\r\n".join(sdp_lines) + "\r\n"
