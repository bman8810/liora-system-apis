"""Unit tests for template-first outbound Weave SMS (no live network)."""

from __future__ import annotations

from dataclasses import asdict
from unittest.mock import MagicMock, call

import pytest

from liora_tools.messaging.idempotency import IdempotencyStore, make_idempotency_key
from liora_tools.messaging.outbound import OutboundSmsSender, SendResult
from liora_tools.messaging.phi import mask_phone, summarize_for_log
from liora_tools.messaging.templates import (
    ZOCDOC_NP_FINGERPRINT,
    ZOCDOC_NP_ROUTE,
    get_template,
    render_template,
)


PHONE = "+15551234567"
FIRST = "Alex"
CORR = "zocdoc-app_test_001"


@pytest.fixture
def store(tmp_path):
    return IdempotencyStore(tmp_path / "idem.json")


@pytest.fixture
def weave():
    client = MagicMock()
    client.send_message.return_value = {
        "smsId": "sms-live-1",
        "threadId": "thr-live-1",
        "personId": "per-1",
    }
    return client


def _sender(store, weave=None, **kwargs) -> OutboundSmsSender:
    return OutboundSmsSender(weave_client=weave, store=store, **kwargs)


def test_1_template_hit_dry_run_default(store, weave):
    """Dry-run default: weave.send_message NOT called; status dry_run."""
    sender = _sender(store, weave, dry_run=True, go_live=False, staged_mock=False)
    result = sender.send(
        route=ZOCDOC_NP_ROUTE,
        vars={"FIRST_NAME": FIRST},
        phone=PHONE,
        correlation_id=CORR,
    )
    assert result.status == "dry_run"
    assert result.mode == "dry_run"
    assert result.template_id
    assert result.body_len and result.body_len > 0
    weave.send_message.assert_not_called()
    # dry_run must not consume idempotency
    assert result.idempotency_key
    assert not store.seen(result.idempotency_key)


def test_2_staged_mock_and_idempotent_second(store, weave):
    """Staged mock succeeds without live call; second call skipped_idempotent."""
    sender = _sender(store, weave, dry_run=False, go_live=False, staged_mock=True)
    r1 = sender.send(
        route=ZOCDOC_NP_ROUTE,
        vars={"FIRST_NAME": FIRST},
        phone=PHONE,
        correlation_id=CORR,
        message_id="msg-1",
    )
    assert r1.status == "staged_mock"
    assert r1.weave_ids.get("smsId")
    weave.send_message.assert_not_called()

    r2 = sender.send(
        route=ZOCDOC_NP_ROUTE,
        vars={"FIRST_NAME": FIRST},
        phone=PHONE,
        correlation_id=CORR,
        message_id="msg-1",
    )
    assert r2.status == "skipped_idempotent"
    weave.send_message.assert_not_called()


def test_3_live_send_once_then_idempotent(store, weave):
    """go_live + dry_run false: send_message once with fingerprint + name."""
    sender = _sender(store, weave, dry_run=False, go_live=True, staged_mock=False)
    r1 = sender.send(
        route=ZOCDOC_NP_ROUTE,
        vars={"FIRST_NAME": FIRST},
        phone=PHONE,
        correlation_id=CORR,
        person_id="per-1",
        message_id="msg-live-1",
    )
    assert r1.status == "sent"
    assert r1.mode == "live"
    weave.send_message.assert_called_once()
    args, kwargs = weave.send_message.call_args
    # phone, body positional
    assert args[0] == PHONE or args[0].endswith("5551234567")
    body = args[1]
    assert ZOCDOC_NP_FINGERPRINT in body
    assert FIRST in body
    assert CORR not in body  # correlation_id must not be in SMS body
    assert kwargs.get("correlation_id") == CORR

    r2 = sender.send(
        route=ZOCDOC_NP_ROUTE,
        vars={"FIRST_NAME": FIRST},
        phone=PHONE,
        correlation_id=CORR,
        person_id="per-1",
        message_id="msg-live-1",
    )
    assert r2.status == "skipped_idempotent"
    assert weave.send_message.call_count == 1


def test_4_template_miss_escalate_even_if_go_live(store, weave):
    sender = _sender(store, weave, dry_run=False, go_live=True, staged_mock=False)
    result = sender.send(
        route="unknown_route_xyz",
        vars={"FIRST_NAME": FIRST},
        phone=PHONE,
        correlation_id=CORR,
    )
    assert result.status == "escalate_to_staff"
    weave.send_message.assert_not_called()


def test_5_template_miss_ai_not_phi_safe(store, weave):
    draft_fn = MagicMock(return_value="please call the office")
    sender = _sender(store, weave, dry_run=False, go_live=True)
    result = sender.send(
        route="missing",
        vars={},
        phone=PHONE,
        allow_ai_draft=True,
        ai_draft_fn=draft_fn,
        phi_safe_for_ai=False,
    )
    assert result.status == "escalate_to_staff"
    assert "not_phi_safe" in result.reason or result.reason == "template_miss_not_phi_safe"
    draft_fn.assert_not_called()
    weave.send_message.assert_not_called()


def test_6_template_miss_ai_phi_safe_still_no_send(store, weave):
    draft_fn = MagicMock(return_value="suggested staff reply text")
    sender = _sender(store, weave, dry_run=False, go_live=True)
    result = sender.send(
        route="missing",
        vars={},
        phone=PHONE,
        allow_ai_draft=True,
        ai_draft_fn=draft_fn,
        phi_safe_for_ai=True,
    )
    assert result.status == "escalate_to_staff"
    draft_fn.assert_called_once()
    # Draft body must not appear on result
    d = asdict(result)
    assert "body" not in d or d.get("body") is None
    assert result.draft_preview_redacted
    assert "suggested staff" not in (result.draft_preview_redacted or "")
    assert "suggested staff" not in (result.reason or "")
    weave.send_message.assert_not_called()


def test_7_blocked_no_go_live(store, weave):
    sender = _sender(store, weave, dry_run=False, go_live=False, staged_mock=False)
    result = sender.send(
        route=ZOCDOC_NP_ROUTE,
        vars={"FIRST_NAME": FIRST},
        phone=PHONE,
    )
    assert result.status == "blocked_no_go_live"
    weave.send_message.assert_not_called()


def test_8_phi_not_in_result_or_log_summary(store, weave):
    sender = _sender(store, weave, dry_run=True)
    result = sender.send(
        route=ZOCDOC_NP_ROUTE,
        vars={"FIRST_NAME": FIRST},
        phone=PHONE,
        correlation_id=CORR,
    )
    d = asdict(result)
    assert "body" not in d
    assert result.phone_masked == mask_phone(PHONE)
    assert result.phone_masked.startswith("***")
    assert PHONE not in result.phone_masked
    assert PHONE not in str(d.values())
    # reason must not carry raw template body with patient name
    assert FIRST not in (result.reason or "")
    summary = summarize_for_log(d)
    assert "body" not in summary
    assert PHONE not in str(summary)


def test_9_invalid_template_vars_refused(store, weave):
    spec = get_template(ZOCDOC_NP_ROUTE)
    assert spec is not None
    with pytest.raises((ValueError, RuntimeError)):
        render_template(spec, {"FIRST_NAME": FIRST, "DOB": "01/01/1990"})
    # Missing required var also fails
    with pytest.raises((ValueError, RuntimeError)):
        render_template(spec, {})
    sender = _sender(store, weave, dry_run=False, go_live=True)
    # Disallowed var at send time → error, no send
    result = sender.send(
        route=ZOCDOC_NP_ROUTE,
        vars={"FIRST_NAME": FIRST, "SSN": "123456789"},
        phone=PHONE,
    )
    assert result.status == "error"
    weave.send_message.assert_not_called()


def test_10_make_idempotency_key_stable():
    a = make_idempotency_key(
        route="zocdoc_new_patient",
        template_id="tid",
        template_version="1",
        message_id="m1",
        correlation_id="c1",
    )
    b = make_idempotency_key(
        route="zocdoc_new_patient",
        template_id="tid",
        template_version="1",
        message_id="m1",
        correlation_id="c1",
    )
    c = make_idempotency_key(
        route="zocdoc_new_patient",
        template_id="tid",
        template_version="1",
        message_id="m2",
        correlation_id="c1",
    )
    assert a == b
    assert a != c
    assert len(a) == 64  # sha256 hex


def test_send_result_asdict_friendly():
    r = SendResult(status="dry_run", mode="dry_run", reason="x")
    d = asdict(r)
    assert d["status"] == "dry_run"
    assert r.as_dict()["status"] == "dry_run"
