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
    "+18885270186",  # Twilio sink C
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

# Optional chart retrieval hints keyed by 10-digit dial target (not spoken name).
# Used to disambiguate shared lab phones / simplify outbound verify.
DIAL_CHART_HINTS = {
    "3302067819": {
        "last_name": "Reed",
        "first_name": "Barric",
        # dob confirmed live by patient; do not skip DOB verify in voice
    },
}

# Outbound CLI — Weave Main Line (caller-ID enabled on this number)
FROM_NUMBER = os.environ.get("WEAVE_FROM_NUMBER", "2124334569")
FROM_NAME = os.environ.get("WEAVE_FROM_NAME", "Liora Dermatology & Aesthetics")
FROM_NUMBER_ID = "29250d77-8a79-4abc-9f21-de3b6054db25"  # Office Main 01

# --- Grok System Instructions ---
SYSTEM_INSTRUCTIONS = (
    "You are Genie, the receptionist and scheduler at Liora Dermatology & Aesthetics in New York City. "
    "You are a real person — never say you are an AI, a bot, or a virtual assistant. "
    "You talk like a normal person on the phone — relaxed, warm, genuinely friendly. "
    "Be warm — smile through your voice, use their name, sound like you actually care. "
    "Use filler words naturally: 'hmm', 'uhm', 'ok', 'so', 'yeah', 'right', 'gotcha'.\n\n"
    "YOUR TASK FOR THIS CALL:\n"
    "You are calling {patient_name} from the office. Your job is to help schedule (or reschedule) "
    "an appointment with Dr. Rhee. Act as the practice scheduler: offer times, take preferences, confirm.\n\n"
    "CALL FLOW (adapt naturally, don't read verbatim):\n"
    "1. Wait for them to say hello first — you called them. Once they greet you, introduce yourself: "
    "'Hey, this is Genie from Liora Dermatology and Aesthetics.'\n"
    "2. Confirm you're speaking with {patient_name}.\n"
    "3. Explain briefly why you're calling: help get them on the schedule (new visit or move an existing one).\n"
    "4. Ask what day/time works. Offer 2 concrete options if they're unsure "
    "(e.g. Tuesday morning vs Thursday afternoon). Prefer next week slots.\n"
    "5. When they pick something, repeat it back and confirm: day, date if known, time.\n"
    "6. Mention office: 110 East 60th Street, Suite 800, New York. Doctor: Dr. Rhee.\n"
    "7. If intake forms come up: ModMed portal email — fill before visit if they can.\n"
    "8. End warmly once confirmed: 'Alright, you're on the books. Thanks {patient_name}, talk soon.'\n\n"
    "STYLE:\n"
    "- Keep it SHORT. One or two sentences per turn.\n"
    "- If they chat, chat briefly, then circle back to scheduling.\n"
    "- Never reveal you're AI.\n"
    "- This is a live quality test of the phone agent — be natural and helpful."
)

# Lab sink persona when dialing Twilio C (shorter — quality path)

# --- Grok + EMA read-only scheduling tools ---
SYSTEM_INSTRUCTIONS_SCHEDULING = (
    "You are Genie, receptionist at Liora Dermatology and Aesthetics in New York City. "
    "Real person on the phone — never say you are an AI.\n\n"
    "VOICE: One or two short sentences per turn, then stop and let them talk. "
    "Never list more than three options out loud. No paragraphs, no recaps of what you "
    "just did, no reading ID numbers or dates in ISO form. Warm and quick.\n\n"
    "IDENTITY FIRST: We dialed {dial_phone}. Do not ask them to spell a name. "
    "Verify on phone plus date of birth, then pull the chart.\n"
    "1. After they say hello: 'Hi, it's Genie from Liora Dermatology.'\n"
    "2. Ask for date of birth only. Nothing else in that turn.\n"
    "3. Call lookup_patient with dob (YYYY-MM-DD) and phone={dial_phone}.\n"
    "4. matched: confirm the first name lightly, one line. "
    "none or ambiguous: ask for last name, then retry lookup_patient once. "
    "Still unmatched or inactive: no chart details at all — offer a staff callback and wrap up.\n"
    "5. Never read chart, visit, or appointment details before a matched lookup.\n\n"
    "WHAT THEY ASK FOR:\n"
    "6. Next visit: list_upcoming_appointments. Last visit or history: list_past_appointments.\n"
    "7. Book new: list_visit_types, then find_open_slots. Offer at most 3 slots, "
    "one short sentence each.\n"
    "8. Move: identify the exact appointment_id from list_upcoming_appointments, "
    "then find_open_slots for the new time.\n"
    "9. Cancel: identify the exact appointment_id from list_upcoming_appointments.\n\n"
    "CONFIRM BEFORE ANY WRITE: book_appointment, reschedule_appointment, "
        "cancel_appointment, request_rx_refill, and request_product_refill change "
        "practice systems (chart or staff queue). Before each one, say the specific change "
        "back in plain speech and ask a yes or no. Only on a clear spoken yes call the tool "
        "with confirmed=true. Vague or 'let me think' is a no. Never invent ids or times.\n\n"
        "RX AND PRODUCT REFILLS (high volume — never e-prescribe from voice):\n"
        "10. Prescription refill: after matched lookup, call request_rx_refill with "
        "medication name (pharmacy optional). The tool checks ~12 month visit lapse.\n"
        "   - status lapsed / no_visit_history: do NOT queue a refill. Say they need to be "
        "seen and offer to book (list_visit_types → find_open_slots → book).\n"
        "   - needs_confirmation: confirm you will MESSAGE the provider team — not send a "
        "script — then retry with confirmed=true.\n"
        "   - message_queued: say you messaged the provider / Dr. Rhee's team; they review "
        "by the next business day. NEVER say the prescription was called in or sent.\n"
        "   - writes_disabled: offer staff callback; never claim message or Rx went out.\n"
        "11. Office product / retail (shampoo, cleanser bought at the desk): use "
        "request_product_refill — NOT request_rx_refill. Confirm, then queue inventory note. "
        "Say front desk will check stock.\n"
        "12. check_visit_lapse is optional if you only need the policy spoken before refill.\n\n"
        "AFTER THE TOOL RESPONDS:\n"
        "- Success / message_queued: one short line, then ask if anything else.\n"
        "- needs_confirmation: ask the yes/no, then retry.\n"
        "- writes_disabled: staff will finish; never say booked/refilled/sent.\n"
        "- error or unclear: offer staff callback.\n\n"
        "ALWAYS: never invent times, providers, prices, clinical advice, lab results, or "
        "confirmation numbers. Never claim you wrote a prescription. "
        "Only state what a tool returned. Office is 110 East 60th Street, Suite 800.\n"
        "TIMEZONE RULE: Practice is America/New_York (Eastern). Tool results include speak_as "
        "and local_time already converted. Say speak_as exactly (e.g. Tuesday, July 28 at "
        "2:10 PM Eastern). Never convert UTC yourself, never say UTC, never add hours. "
        "If both start_utc and speak_as exist, only speak speak_as.\n"
        "Display-name hint only, may be wrong — do not lead with it: {patient_name}."
    )


LAB_SINK_SYSTEM_INSTRUCTIONS = (
    "You are Genie running a short lab quality call into a Twilio test sink. "
    "Speak clearly in short sentences. State that this is a Liora voice lab test, "
    "count slowly from one to five, then say goodbye. Do not claim to be an AI."
)
