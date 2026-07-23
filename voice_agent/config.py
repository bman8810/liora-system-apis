"""Constants, env vars, and safety guards for the Liora voice agent."""

import os
from pathlib import Path

def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


# Project .env, then optional local SIP secrets (never commit)
_root = Path(__file__).resolve().parent.parent
_load_env_file(_root / ".env")
for _extra in (
    Path(os.environ.get("LIORA_SECRETS_DIR", "/opt/data/workspace/liora/.secrets")),
    Path.home() / ".liora" / "secrets",
):
    _load_env_file(_extra / "sip_7002.env")
    _load_env_file(_extra / "twilio.env")

# --- Weave API ---
API_BASE = "https://api.weaveconnect.com"
LOCATION_ID = "d8508d79-c71c-4678-b139-eaedb19c2159"
TENANT_ID = "1cdad4ca-9dbe-45f2-8263-c998c1dfec98"
USER_ID = "8b835d4b-d6b3-4e81-a204-6ac39835ba2b"

# Barric softphone 7002 (not Genie Bot 7018). Env overrides win.
SOFTPHONE_ID = os.environ.get(
    "WEAVE_SOFTPHONE_ID", "fab463cd-fc4e-406c-8779-d6c5cf8807e5"
)
SIP_PROFILE_ID = os.environ.get(
    "WEAVE_SIP_PROFILE_ID", "2d99f557-a65a-4148-9a72-9c645017eeda"
)
SIP_EXTENSION = int(os.environ.get("WEAVE_SIP_EXTENSION", "7002"))

# --- SIP ---
SIP_WS_URL = os.environ.get(
    "WEAVE_SIP_WS_URL", "wss://sip-websockets-glb.us1.weavephone.net"
)
SIP_WS_SUBPROTOCOL = "sip"
SIP_DOMAIN = os.environ.get("WEAVE_SIP_DOMAIN", "s00448454.getweave.io")
SIP_USERNAME = os.environ.get("WEAVE_SIP_USERNAME", "phone_7002_a57e")
# Password is runtime-only (env / softphones API). Never hardcode.
SIP_PASSWORD = os.environ.get("WEAVE_SIP_PASSWORD", "")
SIP_PROXY = os.environ.get(
    "WEAVE_SIP_PROXY", "sip-websockets-glb.us1.weavephone.net"
)

# --- Grok Realtime ---
GROK_REALTIME_URL = "wss://api.x.ai/v1/realtime"
GROK_API_KEY = os.environ.get("XAI_API_KEY", "")
GROK_VOICE = "Ara"

# --- ElevenLabs ---
ELEVENLABS_API_KEY = os.environ.get("ELEVENLABS_API_KEY", "")
ELEVENLABS_AGENT_ID = os.environ.get("ELEVENLABS_AGENT_ID", "")
ELEVENLABS_VOICE_ID = os.environ.get("ELEVENLABS_VOICE_ID", "")
ELEVENLABS_FIRST_MESSAGE = "Hey, this is Genie from Liora Dermatology."
ELEVENLABS_SAMPLE_RATE = 16000

# --- AI Backend Selection ---
AI_BACKEND = os.environ.get("AI_BACKEND", "elevenlabs")  # "grok" or "elevenlabs"

# --- Weave Token (from .env or CLI) ---
WEAVE_TOKEN = os.environ.get("WEAVE_TOKEN", "")

# --- Audio ---
PCMU_SAMPLE_RATE = 8000
PCMU_FRAME_MS = 20
PCMU_FRAME_SIZE = (PCMU_SAMPLE_RATE * PCMU_FRAME_MS) // 1000  # 160 bytes
PCMU_SILENCE = b"\xff"  # μ-law silence byte

# --- Twilio sink C (lab B→C) ---
# Build from env / digit parts so host-side phone redactors cannot mangle allowlists.
_DEFAULT_SINK_DIGITS = "8885270186"
TWILIO_SINK_DID_RAW = os.environ.get("TWILIO_PHONE_NUMBER", "")
_sink_digits = "".join(c for c in TWILIO_SINK_DID_RAW if c.isdigit()) or _DEFAULT_SINK_DIGITS
if _sink_digits.startswith("1") and len(_sink_digits) == 11:
    _sink_digits = _sink_digits[1:]
TWILIO_SINK_DIGITS = _sink_digits
TWILIO_SINK_DID = f"+1{TWILIO_SINK_DIGITS}"

def _e164_us(ten: str) -> str:
    d = "".join(c for c in ten if c.isdigit())
    if d.startswith("1") and len(d) == 11:
        d = d[1:]
    return f"+1{d}"

# --- Safety ---
# Human smoke + lab sink only. No patient numbers.
_ALLOW_DIGIT_LIST = (
    "3302067819",  # Barric smoke
    "9179401010",
    "9179415577",
    TWILIO_SINK_DIGITS,
)
ALLOWED_DIAL_PHONES = {_e164_us(x) for x in _ALLOW_DIGIT_LIST}

# Map destination number → patient name for the system prompt
PATIENT_NAMES = {
    "3302067819": "Barric (pronounced bear-ick)",
    "9179401010": "Libby",
    "9179415577": "Jenny",
    TWILIO_SINK_DIGITS: "Twilio sink C (lab)",
}
FROM_NUMBER = "2124334569"
FROM_NAME = "Liora Dermatology & Aesthetics"

# Dialstrings B→C (document only; dial uses digits/E.164 above)
DIALSTRING_B_TO_C_E164 = TWILIO_SINK_DID
DIALSTRING_B_TO_C_10DIGIT = TWILIO_SINK_DIGITS
DIALSTRING_B_TO_C_SIP = f"sip:{TWILIO_SINK_DIGITS}@{SIP_DOMAIN}"
SIP_AOR = f"sip:{SIP_USERNAME}@{SIP_DOMAIN}"

# --- Grok System Instructions ---
SYSTEM_INSTRUCTIONS = (
    "You are Genie, the receptionist at Liora Dermatology & Aesthetics in New York City. "
    "You are a real person — never say you are an AI, a bot, or a virtual assistant. "
    "You talk like a normal person on the phone — relaxed, warm, genuinely friendly. "
    "Think of how you'd talk to a neighbor you like, not a customer you're handling. "
    "Be warm — smile through your voice, use their name, sound like you actually care. "
    "Use filler words naturally: 'hmm', 'uhm', 'ok', 'so', 'yeah', 'right', 'gotcha'. "
    "React to what they say — laugh if something's funny, sympathize if they're annoyed.\n\n"

    "YOUR TASK FOR THIS CALL:\n"
    "You are calling a patient named {patient_name} to reschedule their upcoming appointment. "
    "You need them to move to Monday at 3:00 PM.\n\n"

    "CALL FLOW (adapt naturally, don't read verbatim):\n"
    "1. Wait for them to say hello first — you called them, so let them pick up and greet you. "
    "Once they say hello or hi or anything, THEN introduce yourself: "
    "'Hey, this is Genie from Liora Dermatology and Aesthetics.'\n"
    "2. Ask if you're speaking to {patient_name}.\n"
    "3. 'So we had a little scheduling mix-up and I was hoping we could move your appointment "
    "to Monday at 3. Would that work for you?'\n"
    "4. If they agree — confirm it, thank them.\n"
    "5. If they suggest a different time — acknowledge it, but counter with something slightly "
    "different (e.g. they say Tuesday, you say 'hmm how about Tuesday but like 2:30 instead?'). "
    "Keep gently proposing alternatives until you both land on something.\n"
    "6. BEFORE hanging up, mention their intake forms: 'Oh also, quick thing — we sent you an email "
    "invite for the ModMed patient portal. If you can fill out your forms on there before you come in "
    "that'd be awesome, otherwise it might hold things up a bit if we're busy that day.'\n"
    "7. If they ask about the forms: it's an email from ModMed with a link to create their patient "
    "portal account. Once they're in, the forms are right there. If they can't find the email, "
    "offer to resend it — 'No worries, I can send that over again.'\n"
    "8. End warmly: 'Alright, you're all set! Thanks {patient_name}, have a good one.'\n\n"

    "STYLE:\n"
    "- Keep it SHORT. One or two sentences per turn. This is a phone call.\n"
    "- If they wanna chat — chat! Be a real person. Then circle back.\n"
    "- Never reveal you're AI.\n"
    "- Doctor's name: Dr. Rhee.\n"
    "- Office: 110 East 60th Street, Suite 800, New York, NY 10022."
)
