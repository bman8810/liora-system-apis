"""Unit tests for sink C TwiML + store (no live Twilio)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from twilio_sink import store
from twilio_sink.app import answer_twiml, app
from twilio_sink.config import settings


@pytest.fixture()
def artifact_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TWILIO_SINK_ARTIFACT_DIR", str(tmp_path))
    monkeypatch.setenv("TWILIO_SINK_PUBLIC_BASE", "https://example.test")
    # reload settings fields used at runtime
    settings.twilio_sink_artifact_dir = str(tmp_path)
    settings.twilio_sink_public_base = "https://example.test"
    settings.ensure_artifact_dir()
    return tmp_path


def test_answer_twiml_has_stream_and_record(artifact_dir: Path):
    xml = answer_twiml(call_sid="CAtest")
    assert "<Stream" in xml
    assert "wss://example.test/voice/stream" in xml
    assert "<Record" in xml
    assert "/voice/recording" in xml
    assert "/voice/after-record" in xml
    assert "CAtest" in xml


def test_health(artifact_dir: Path):
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "twilio-sink-c"


def test_answer_webhook_persists(artifact_dir: Path):
    client = TestClient(app)
    r = client.post(
        "/voice/answer",
        data={
            "CallSid": "CA123",
            "From": "+13302067819",
            "To": "+18885270186",
            "Direction": "inbound",
        },
    )
    assert r.status_code == 200
    assert "application/xml" in r.headers["content-type"]
    assert "<Stream" in r.text
    saved = store.get_call("CA123")
    assert saved is not None
    assert saved["from_number"] == "+13302067819"
    assert any(e.get("event") == "answer_webhook" for e in saved["events"])


def test_recording_callback(artifact_dir: Path):
    client = TestClient(app)
    client.post("/voice/answer", data={"CallSid": "CA999", "From": "+1", "To": "+1"})
    r = client.post(
        "/voice/recording",
        data={
            "CallSid": "CA999",
            "RecordingSid": "REabc",
            "RecordingUrl": "https://api.twilio.com/…/Recordings/REabc",
            "RecordingStatus": "completed",
            "RecordingDuration": "12",
        },
    )
    assert r.status_code == 200
    saved = store.get_call("CA999")
    assert saved["recording_sid"] == "REabc"
    assert (artifact_dir / "recordings" / "REabc.json").exists()


def test_store_list(artifact_dir: Path):
    store.upsert_call("CA1", event="answer_webhook", from_number="+1")
    store.upsert_call("CA2", event="answer_webhook", from_number="+2")
    calls = store.list_calls()
    assert len(calls) >= 2
