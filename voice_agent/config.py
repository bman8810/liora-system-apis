"""Constants, env vars, and safety guards for the Liora voice agent.

Voice product B (Barric softphone 7002) → lab sink C (Twilio DID).
Softphone IDs default to Barric; runtime fetch in auth.py reconfirms them.
"""

import os
from pathlib import Path

# Load .env file from project root if present
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    for line in _env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())

# --- Weave API ---
API_BASE = "https://api.weaveconnect.com"
LOCATION_ID = "d8508d79-c71c-4678-b139-eaedb19c2159"
TENANT_ID = "1cdad4ca-9dbe-45f2-8263-c998c1dfec98"
# Barric Reed practice user (not Genie Bot). JWT user_id refreshes per login.
USER_ID = os.environ.get("WEAVE_USER_ID", "2a3680fd-7e02-49f2-8bff-506fe1e54a0f")

# Barric R softphone 7002 (live softphones/settings 2026-07-23).
# Genie Bot 7018 kept only as SOFTPHONE_ID_GENIE / SIP_PROFILE_ID_GENIE below.
SOFTPHONE_ID = os.environ.get("WEAVE_SOFTPHONE_ID", "fab463cd-fc4e-406c-8779-d6c5cf8807e5")
SIP_PROFILE_ID = os.environ.get("WEAVE_SIP_PROFILE_ID", "2d99f557-a65a-4148-9a72-9c645017eeda")
TARGET_SOFTPHONE_EXTENSION = int(os.environ.get("WEAVE_SOFTPHONE_EXT", "7002"))

# Legacy Genie Bot (do not use for product path B)
SOFTPHONE_ID_GENIE = "dd2b2484-f5f0-43d2-8029-9a140f958fed"
SIP_PROFILE_ID_GENIE = "c6d657dc-fbdd-47bd-b6e6-bc055dcd3346"

# --- SIP ---
SIP_WS_URL = "wss://sip-websockets-glb.us1.weavephone.net"
SIP_WS_SUBPROTOCOL = "sip"
SIP_DOMAIN = "s00448454.getweave.io"
SIP_USERNAME = os.environ.get("WEAVE_SIP_USERNAME", "phone_7002_a57e")
SIP_EXTENSION = TARGET_SOFTPHONE_EXTENSION

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
AI_BACKEND = os.environ.get("AI_BACKEND", "grok")  # "grok" or "elevenlabs" — lab path prefers Grok

# --- Weave Token (from .env or CLI) ---
WEAVE_TOKEN = os.environ.get("WEAVE_TOKEN", "")

# --- Audio ---
PCMU_SAMPLE_RATE = 8000
PCMU_FRAME_MS = 20
PCMU_FRAME_SIZE = (PCMU_SAMPLE_RATE * PCMU_FRAME_MS) // 1000  # 160 bytes
PCMU_SILENCE = b"\xff"  # μ-law silence byte

# --- Lab sink C (Twilio DID) ---
# Product path B dials this DID; inbound answer is Twilio sink C (sibling task).
# Not a Weave-hosted number — association is outbound lab target on voice B.
TWILIO_SINK_DID = os.environ.get("TWILIO_PHONE_NUMBER", "+18885270186")
LAB_DIAL_DEFAULT = TWILIO_SINK_DID  # default --dial when lab mode set

# --- Safety ---
# Smoke human + Twilio sink C only for automated lab. Expand only with Barric OK.
ALLOWED_DIAL_PHONES = {
    "+13302067819",  # Barric smoke
    "+18885270186",  # Twilio sink C (voice product path B→C)
    "+19179401010",  # Libby (manual only)
    "+19179415577",  # Jenny (manual only)
}
# Always include env TWILIO_PHONE_NUMBER if set to a different E.164
if TWILIO_SINK_DID and TWILIO_SINK_DID not in ALLOWED_DIAL_PHONES:
    ALLOWED_DIAL_PHONES = set(ALLOWED_DIAL_PHONES) | {TWILIO_SINK_DID}

# Map destination number → patient/lab name for the system prompt
PATIENT_NAMES = {
    "3302067819": "Barric (pronounced bear-ick)",
    "8885270186": "Twilio lab sink C",
    "9179401010": "Libby",
    "9179415577": "Jenny",
}

# Outbound CLI — Weave Main Line (caller-ID enabled on this number)
FROM_NUMBER = os.environ.get("WEAVE_FROM_NUMBER", "2124334569")
FROM_NAME = os.environ.get("WEAVE_FROM_NAME", "Liora Dermatology & Aesthetics")
FROM_NUMBER_ID = "29250d77-8a79-4abc-9f21-de3b6054db25"  # Office Main 01

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

# Lab sink persona when dialing Twilio C (shorter — quality path)
LAB_SINK_SYSTEM_INSTRUCTIONS = (
    "You are Genie running a short lab quality call into a Twilio test sink. "
    "Speak clearly in short sentences. State that this is a Liora voice lab test, "
    "count slowly from one to five, then say goodbye. Do not claim to be an AI."
)
