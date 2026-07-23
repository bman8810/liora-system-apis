# Twilio sink C — runbook (Liora)

Lab path **C**: Twilio DID answers, starts Media Stream, records. Weave **B** dials this DID later.

## Code

| | |
|--|--|
| Package | `liora-system-apis/twilio_sink/` |
| Entry | `python -m twilio_sink` |
| Configure DID | `python -m twilio_sink.configure_number` |
| Branch | `feat/twilio-sink-c` |

## Env vars (never commit values)

Read from process env (Hermes `$HERMES_HOME/.env` and/or `/opt/data/workspace/liora/.secrets/twilio.env`).

| Var | Purpose |
|-----|---------|
| `TWILIO_ACCOUNT_SID` | Account |
| `TWILIO_API_KEY_SID` + `TWILIO_API_KEY_SECRET` | REST auth (preferred) |
| `TWILIO_AUTH_TOKEN` | REST auth alternative |
| `TWILIO_PHONE_NUMBER` | Sink DID E.164 |
| `TWILIO_SINK_PUBLIC_BASE` | Public `https://…` base (no trailing slash) |
| `TWILIO_SINK_PORT` | Local bind (default `8090`) |
| `TWILIO_SINK_ARTIFACT_DIR` | Call/recording metadata store |
| `TWILIO_SINK_HOLD_SECONDS` | Post-record pause budget |
| `TWILIO_PROBE_TO` | Default probe callee (`+13302067819`) |
| `TWILIO_SINK_ADMIN_TOKEN` | Optional gate on `/voice/calls*` |

## HTTP surface

| Method | Path | Role |
|--------|------|------|
| GET | `/health` | Liveness + config flags (no secrets) |
| POST | `/voice/answer` | Inbound/outbound TwiML: `<Start><Stream>` + `<Record>` |
| POST | `/voice/status` | Call status callback |
| POST | `/voice/recording` | Recording status → artifact pointer |
| WS | `/voice/stream` | Media Streams (mulaw frames counted; mark on first media) |
| GET | `/voice/calls` | Recent call JSON (lab) |

## Artifacts

Default dir: `/opt/data/workspace/liora/cache/twilio-sink/`

```
calls/<CallSid>.json       # webhook + stream + recording events
recordings/<RecordingSid>.json  # pointer to Twilio RecordingUrl (fetch with Basic auth)
last_configure.json        # last DID/probe summary (no secrets)
```

Recording **media** stays in Twilio until explicitly downloaded; do not commit audio or dump PHI to git/Telegram.

## Local + tunnel (Hermes)

```bash
cd /opt/data/workspace/liora/liora-system-apis
set -a
source /opt/data/.env
source /opt/data/workspace/liora/.secrets/twilio.env
set +a
export TWILIO_SINK_ARTIFACT_DIR=/opt/data/workspace/liora/cache/twilio-sink
export TWILIO_SINK_PORT=8090

# terminal A
python -m twilio_sink

# terminal B — quick tunnel
/opt/data/bin/cloudflared tunnel --url http://127.0.0.1:8090
# copy https://*.trycloudflare.com → TWILIO_SINK_PUBLIC_BASE

export TWILIO_SINK_PUBLIC_BASE=https://YOUR_SUBDOMAIN.trycloudflare.com
# restart sink so TwiML embeds the wss URL, then:
python -m twilio_sink.configure_number --probe --to +13302067819
```

`configure_number` sets DID `VoiceUrl` → `{base}/voice/answer` and `StatusCallback` → `{base}/voice/status`.

## Proof checklist

1. `GET {base}/health` → `status=ok`, `public_base` set  
2. DID `voice_url` points at `/voice/answer` (Twilio console or REST)  
3. Probe or inbound call creates `calls/CA….json` with `answer_webhook`  
4. Same file shows `stream` event with `media_frames > 0` (when far end answers with audio path)  
5. `recording` event or `recordings/RE….json` with `recording_status=completed`  
6. No secrets in repo, cards, or runbook values  

## Notes

- Messaging on this DID is **not** product path (Barric). Voice only.  
- Grok Realtime bridge on the stream is a follow-up; sink C smoke is webhook + stream + record.  
- Sibling B→C E2E card owns Weave SIP dial into this DID once B/SIP ready.  
