"""FastAPI app: Twilio voice answer, media stream WS, recording callbacks."""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from xml.sax.saxutils import escape

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, Query, Header, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from twilio_sink import store
from twilio_sink.config import settings

logger = logging.getLogger("twilio_sink")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

app = FastAPI(title="Liora Twilio Sink C", version="0.1.0")


def _require_admin(token: str | None) -> None:
    expected = settings.twilio_sink_admin_token
    if not expected:
        return
    if not token or token != expected:
        raise HTTPException(status_code=401, detail="admin token required")


def _ws_url() -> str:
    base = settings.public_base()
    if not base:
        # Relative won't work for Twilio Stream; return placeholder (still valid XML for local curl)
        return "wss://localhost/voice/stream"
    if base.startswith("https://"):
        return "wss://" + base[len("https://") :] + "/voice/stream"
    if base.startswith("http://"):
        return "ws://" + base[len("http://") :] + "/voice/stream"
    return base.rstrip("/") + "/voice/stream"


def _http_url(path: str) -> str:
    base = settings.public_base()
    path = path if path.startswith("/") else f"/{path}"
    if not base:
        return path
    return base.rstrip("/") + path


def answer_twiml(call_sid: str = "") -> str:
    """TwiML: start media stream (both tracks), greet, record once, hang up."""
    stream_url = escape(_ws_url())
    rec_cb = escape(_http_url("/voice/recording"))
    # IMPORTANT: Record without action re-requests the current document URL → answer loop.
    after_record = escape(_http_url("/voice/after-record"))
    rec_max = max(10, int(settings.twilio_sink_record_max_seconds))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Start>
    <Stream url="{stream_url}" track="both_tracks">
      <Parameter name="sink" value="liora-c" />
      <Parameter name="callSid" value="{escape(call_sid or "")}" />
    </Stream>
  </Start>
  <Say voice="Polly.Joanna">Liora lab sink C. This call is recorded for quality tests.</Say>
  <Record
    playBeep="false"
    maxLength="{rec_max}"
    action="{after_record}"
    method="POST"
    recordingStatusCallback="{rec_cb}"
    recordingStatusCallbackMethod="POST"
    trim="trim-silence"
    timeout="4"
  />
  <Redirect method="POST">{after_record}</Redirect>
</Response>
"""


@app.post("/voice/after-record")
async def voice_after_record(request: Request) -> Response:
    """Terminal TwiML after <Record> so answer webhook is not re-entered."""
    form = await request.form()
    data = {k: form.get(k) for k in form.keys()}
    call_sid = str(data.get("CallSid") or "")
    store.upsert_call(
        call_sid,
        event="after_record",
        digits=str(data.get("Digits") or ""),
        recording_url=str(data.get("RecordingUrl") or ""),
        recording_sid=str(data.get("RecordingSid") or "") or None,
        recording_duration=str(data.get("RecordingDuration") or ""),
    )
    logger.info("after-record CallSid=%s RecordingSid=%s", call_sid, data.get("RecordingSid"))
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Response>
  <Say voice="Polly.Joanna">Recording complete. Goodbye.</Say>
  <Hangup/>
</Response>
"""
    return Response(content=xml, media_type="application/xml")


@app.on_event("startup")
async def _startup() -> None:
    settings.ensure_artifact_dir()
    logger.info(
        "twilio_sink starting port=%s public_base=%s artifacts=%s rest_auth=%s",
        settings.twilio_sink_port,
        settings.public_base() or "(unset)",
        settings.twilio_sink_artifact_dir,
        settings.has_rest_auth(),
    )


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "service": "twilio-sink-c",
        "public_base": settings.public_base() or None,
        "stream_url": _ws_url() if settings.public_base() else None,
        "artifact_dir": settings.twilio_sink_artifact_dir,
        "rest_auth_configured": settings.has_rest_auth(),
        "phone": settings.twilio_phone_number or None,
    }


@app.post("/voice/answer")
async def voice_answer(request: Request) -> Response:
    form = await request.form()
    data = {k: form.get(k) for k in form.keys()}
    call_sid = str(data.get("CallSid") or "")
    from_n = str(data.get("From") or "")
    to_n = str(data.get("To") or "")
    direction = str(data.get("Direction") or "")
    store.upsert_call(
        call_sid,
        event="answer_webhook",
        from_number=from_n,
        to_number=to_n,
        direction=direction,
        answered_at=time.time(),
        webhook_path="/voice/answer",
    )
    logger.info("answer CallSid=%s From=%s To=%s Direction=%s", call_sid, from_n, to_n, direction)
    xml = answer_twiml(call_sid=call_sid)
    return Response(content=xml, media_type="application/xml")


@app.post("/voice/status")
async def voice_status(request: Request) -> PlainTextResponse:
    form = await request.form()
    data = {k: form.get(k) for k in form.keys()}
    call_sid = str(data.get("CallSid") or "")
    store.upsert_call(
        call_sid,
        event="status",
        call_status=str(data.get("CallStatus") or ""),
        call_duration=str(data.get("CallDuration") or data.get("Duration") or ""),
        sequence_number=str(data.get("SequenceNumber") or ""),
    )
    logger.info(
        "status CallSid=%s CallStatus=%s Duration=%s",
        call_sid,
        data.get("CallStatus"),
        data.get("CallDuration") or data.get("Duration"),
    )
    return PlainTextResponse("ok")


@app.post("/voice/recording")
async def voice_recording(request: Request) -> PlainTextResponse:
    form = await request.form()
    data = {k: form.get(k) for k in form.keys()}
    call_sid = str(data.get("CallSid") or "")
    rec_sid = str(data.get("RecordingSid") or "")
    rec_url = str(data.get("RecordingUrl") or "")
    rec_status = str(data.get("RecordingStatus") or "")
    rec_duration = str(data.get("RecordingDuration") or "")
    # Persist metadata only; media stays at Twilio until explicitly downloaded.
    store.mark_recording(
        call_sid,
        recording_sid=rec_sid,
        recording_url=rec_url,
        recording_status=rec_status,
        recording_duration=rec_duration,
        recording_channels=str(data.get("RecordingChannels") or ""),
    )
    # Also write a pointer file under recordings/
    if rec_sid:
        root = settings.ensure_artifact_dir() / "recordings"
        pointer = {
            "call_sid": call_sid,
            "recording_sid": rec_sid,
            "recording_url": rec_url,
            "recording_status": rec_status,
            "recording_duration": rec_duration,
            "at": time.time(),
        }
        (root / f"{rec_sid}.json").write_text(json.dumps(pointer, indent=2))
    logger.info(
        "recording CallSid=%s RecordingSid=%s status=%s duration=%s",
        call_sid,
        rec_sid,
        rec_status,
        rec_duration,
    )
    return PlainTextResponse("ok")


@app.websocket("/voice/stream")
async def voice_stream(ws: WebSocket) -> None:
    await ws.accept()
    call_sid = ""
    stream_sid = ""
    media_frames = 0
    inbound_bytes = 0
    started_at = time.time()
    try:
        while True:
            message = await ws.receive_text()
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                continue
            event = payload.get("event")
            if event == "connected":
                logger.info("stream connected protocol=%s", payload.get("protocol"))
            elif event == "start":
                start = payload.get("start") or {}
                call_sid = str(start.get("callSid") or payload.get("callSid") or "")
                stream_sid = str(start.get("streamSid") or payload.get("streamSid") or "")
                tracks = start.get("tracks") or []
                media_format = start.get("mediaFormat") or {}
                store.append_stream_meta(
                    call_sid,
                    stream_sid=stream_sid,
                    stream_status="started",
                    tracks=tracks,
                    media_format=media_format,
                    custom_parameters=start.get("customParameters") or {},
                )
                logger.info(
                    "stream start CallSid=%s StreamSid=%s tracks=%s",
                    call_sid,
                    stream_sid,
                    tracks,
                )
            elif event == "media":
                media = payload.get("media") or {}
                chunk = media.get("payload") or ""
                # base64 mulaw payload size proxy
                inbound_bytes += len(chunk)
                media_frames += 1
                # Echo first ~2s of outbound track is optional; for smoke we count only.
                # Optionally mark mark events so Twilio timeline shows stream alive.
                if media_frames == 1 and stream_sid:
                    try:
                        await ws.send_text(
                            json.dumps(
                                {
                                    "event": "mark",
                                    "streamSid": stream_sid,
                                    "mark": {"name": "sink-c-first-media"},
                                }
                            )
                        )
                    except Exception:
                        pass
            elif event == "mark":
                store.append_stream_meta(call_sid or "unknown", mark=payload.get("mark"))
            elif event == "stop":
                duration = time.time() - started_at
                store.append_stream_meta(
                    call_sid or "unknown",
                    stream_status="stopped",
                    stream_sid=stream_sid,
                    media_frames=media_frames,
                    media_payload_b64_chars=inbound_bytes,
                    stream_duration_sec=round(duration, 3),
                )
                logger.info(
                    "stream stop CallSid=%s frames=%s b64_chars=%s dur=%.2fs",
                    call_sid,
                    media_frames,
                    inbound_bytes,
                    duration,
                )
                break
    except WebSocketDisconnect:
        duration = time.time() - started_at
        store.append_stream_meta(
            call_sid or "unknown",
            stream_status="disconnect",
            stream_sid=stream_sid,
            media_frames=media_frames,
            media_payload_b64_chars=inbound_bytes,
            stream_duration_sec=round(duration, 3),
        )
        logger.info(
            "stream disconnect CallSid=%s frames=%s",
            call_sid,
            media_frames,
        )


@app.get("/voice/calls")
async def list_calls(
    limit: int = Query(20, ge=1, le=200),
    x_admin_token: str | None = Header(default=None),
) -> JSONResponse:
    _require_admin(x_admin_token)
    return JSONResponse({"calls": store.list_calls(limit=limit)})


@app.get("/voice/calls/{call_sid}")
async def get_call(
    call_sid: str,
    x_admin_token: str | None = Header(default=None),
) -> JSONResponse:
    _require_admin(x_admin_token)
    data = store.get_call(call_sid)
    if not data:
        raise HTTPException(status_code=404, detail="call not found")
    return JSONResponse(data)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "twilio-sink-c", "health": "/health"}
