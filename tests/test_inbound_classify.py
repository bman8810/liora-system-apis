"""Table-driven fixtures for inbound classify/route (no live Weave)."""

from __future__ import annotations

import json

import pytest

from liora_tools.messaging import (
    ROUTE_ESCALATE,
    ROUTE_REFILL,
    ROUTE_SCHEDULE,
    ROUTE_ZOCDOC_NP,
    NormalizedInboundMessage,
    RouteDecision,
    classify_inbound,
    decision_log_dict,
)
from liora_tools.messaging.classify import (
    HANDLER_ESCALATE,
    HANDLER_REFILL,
    HANDLER_SCHEDULE,
    HANDLER_ZOCDOC_NP,
    ZOCDOC_NP_OUTBOUND_FINGERPRINT,
    body_fingerprint,
    redact_text_for_log,
)


def _msg(
    body: str,
    *,
    message_id: str = "m1",
    thread_id: str = "t1",
    fingerprints: tuple[str, ...] = (),
    direction: str = "inbound",
    phone_last4: str | None = "1212",
) -> NormalizedInboundMessage:
    return NormalizedInboundMessage(
        message_id=message_id,
        thread_id=thread_id,
        body=body,
        direction=direction,
        person_phone_last4=phone_last4,
        person_id="person-abc",
        body_preview="[redacted]",
        prior_outbound_fingerprints=fingerprints,
    )


# (id, body, expected_route, min_confidence, staff_escalation, fingerprints)
FIXTURES: list[tuple[str, str, str, float, bool, tuple[str, ...]]] = [
    # ── Zocdoc NP ──────────────────────────────────────────────────────────
    (
        "np_portal_resend",
        "Hi can you resend the portal link please?",
        ROUTE_ZOCDOC_NP,
        0.7,
        False,
        (),
    ),
    (
        "np_zocdoc_word",
        "I booked on Zocdoc and need help finishing registration",
        ROUTE_ZOCDOC_NP,
        0.7,
        False,
        (),
    ),
    (
        "np_card_on_file",
        "Do I really need a credit card on file before you confirm?",
        ROUTE_ZOCDOC_NP,
        0.7,
        False,
        (),
    ),
    (
        "np_fee_language",
        "What is the booking cost of $100 about?",
        ROUTE_ZOCDOC_NP,
        0.7,
        False,
        (),
    ),
    (
        "np_thread_prior_fp_plus_portal",
        "I can't log into the portal",
        ROUTE_ZOCDOC_NP,
        0.75,
        False,
        (ZOCDOC_NP_OUTBOUND_FINGERPRINT,),
    ),
    # ── Schedule ───────────────────────────────────────────────────────────
    (
        "sched_reschedule",
        "I need to reschedule my appointment next week",
        ROUTE_SCHEDULE,
        0.65,
        False,
        (),
    ),
    (
        "sched_cancel",
        "Please cancel my appt on Friday",
        ROUTE_SCHEDULE,
        0.65,
        False,
        (),
    ),
    (
        "sched_book",
        "Can I book a follow-up with the doctor?",
        ROUTE_SCHEDULE,
        0.65,
        False,
        (),
    ),
    (
        "sched_availability",
        "What is the next available opening for a visit?",
        ROUTE_SCHEDULE,
        0.65,
        False,
        (),
    ),
    (
        "sched_when_is_my",
        "When is my appointment?",
        ROUTE_SCHEDULE,
        0.65,
        False,
        (),
    ),
    # ── Refill ─────────────────────────────────────────────────────────────
    (
        "refill_rx",
        "I need a refill on my prescription cream",
        ROUTE_REFILL,
        0.7,
        False,
        (),
    ),
    (
        "refill_pharmacy",
        "Can you send my rx to the pharmacy?",
        ROUTE_REFILL,
        0.7,
        False,
        (),
    ),
    (
        "refill_ran_out",
        "I ran out of my medication",
        ROUTE_REFILL,
        0.7,
        False,
        (),
    ),
    (
        "refill_tretinoin",
        "Need more tretinoin please",
        ROUTE_REFILL,
        0.7,
        False,
        (),
    ),
    # ── Escalate (safe default) ────────────────────────────────────────────
    (
        "esc_empty",
        "   ",
        ROUTE_ESCALATE,
        0.0,
        True,
        (),
    ),
    (
        "esc_unknown",
        "asdf qwerty blue banana hello there friend",
        ROUTE_ESCALATE,
        0.0,
        True,
        (),
    ),
    (
        "esc_ack_only",
        "Thanks!",
        ROUTE_ESCALATE,
        0.0,
        True,
        (),
    ),
    (
        "esc_call_me_back",
        "Please call me back when you can",
        ROUTE_ESCALATE,
        0.9,
        True,
        (),
    ),
    (
        "esc_human",
        "I need to speak to a real person",
        ROUTE_ESCALATE,
        0.9,
        True,
        (),
    ),
    (
        "esc_urgent",
        "This is urgent — my face is swelling",
        ROUTE_ESCALATE,
        0.9,
        True,
        (),
    ),
    (
        "esc_multi_intent",
        "Please refill my rx and also reschedule my appointment",
        ROUTE_ESCALATE,
        0.55,
        True,
        (),
    ),
    (
        "esc_non_inbound",
        "reschedule my appointment",
        ROUTE_ESCALATE,
        0.0,
        True,
        (),
    ),  # direction overridden in dedicated test
]


@pytest.mark.parametrize(
    "fid,body,route,min_conf,staff,fps",
    [f for f in FIXTURES if f[0] != "esc_non_inbound"],
    ids=[f[0] for f in FIXTURES if f[0] != "esc_non_inbound"],
)
def test_classify_fixtures(fid, body, route, min_conf, staff, fps):
    d = classify_inbound(_msg(body, fingerprints=fps, message_id=fid))
    assert d.route_key == route, f"{fid}: got {d.route_key} reason={d.reason}"
    assert d.staff_escalation_required is staff
    assert d.confidence >= min_conf
    assert d.handler_hint
    assert d.reason


def test_non_inbound_direction_escalates():
    d = classify_inbound(
        _msg("reschedule my appointment", direction="outbound")
    )
    assert d.route_key == ROUTE_ESCALATE
    assert d.reason == "non_inbound_direction"
    assert d.staff_escalation_required is True


def test_raw_string_input():
    d = classify_inbound("I need to cancel my appointment tomorrow")
    assert d.route_key == ROUTE_SCHEDULE
    assert d.handler_hint == HANDLER_SCHEDULE


def test_mapping_input():
    d = classify_inbound(
        {
            "body": "Need a refill please",
            "message_id": "x",
            "thread_id": "y",
            "direction": "inbound",
        }
    )
    assert d.route_key == ROUTE_REFILL
    assert d.handler_hint == HANDLER_REFILL


def test_handler_hints_stable():
    assert classify_inbound("portal link please").handler_hint == HANDLER_ZOCDOC_NP
    assert classify_inbound("book appointment").handler_hint == HANDLER_SCHEDULE
    assert classify_inbound("rx refill").handler_hint == HANDLER_REFILL
    assert classify_inbound("???").handler_hint == HANDLER_ESCALATE


def test_multi_intent_lists_secondary_routes():
    d = classify_inbound(
        "Please refill my prescription and also cancel my appointment"
    )
    assert d.route_key == ROUTE_ESCALATE
    assert d.reason == "multi_intent"
    assert ROUTE_REFILL in d.secondary_routes
    assert ROUTE_SCHEDULE in d.secondary_routes


def test_decision_to_dict_roundtrip_shape():
    d = classify_inbound("reschedule please")
    payload = d.to_dict()
    assert payload["route_key"] == ROUTE_SCHEDULE
    assert "confidence" in payload
    assert isinstance(payload["matched_rules"], list)


def test_decision_log_dict_no_phi():
    body = (
        "Hi Jane Doe, call me at +1 (212) 555-0199 or jane.doe@example.com "
        "DOB 01/15/1990 — need portal link"
    )
    msg = _msg(body, message_id="phi-1", thread_id="thr-9", phone_last4="0199")
    d = classify_inbound(msg)
    log = decision_log_dict(d, msg)
    blob = json.dumps(log)
    assert "Jane" not in blob
    assert "212" not in blob
    assert "555-0199" not in blob
    assert "example.com" not in blob
    assert "01/15/1990" not in blob
    assert body not in blob
    assert "portal link" not in blob  # body content must not leak
    assert log["message_id"] == "phi-1"
    assert log["thread_id"] == "thr-9"
    assert log["body_len"] == len(body)
    assert log["body_fp"] == body_fingerprint(body)
    assert log["phone_last4"] == "0199"
    assert "body" not in log
    assert "text" not in log


def test_redact_text_for_log():
    s = redact_text_for_log("email me at a@b.co or +12125550199 dob 03/04/1991")
    assert "[email]" in s
    assert "[phone]" in s
    assert "[dob]" in s
    assert "a@b.co" not in s


def test_empty_and_whitespace_escalate():
    for body in ("", " ", "\n\t"):
        d = classify_inbound(body)
        assert d.route_key == ROUTE_ESCALATE
        assert d.staff_escalation_required


def test_route_decision_staff_flag_for_handlers():
    np = classify_inbound("resend portal link")
    assert np.staff_escalation_required is False
    esc = classify_inbound("talk to a human")
    assert esc.staff_escalation_required is True
    assert isinstance(esc, RouteDecision)


def test_unknown_low_confidence_is_escalate_default():
    d = classify_inbound("purple widgets inventory sync")
    assert d.route_key == ROUTE_ESCALATE
    assert d.staff_escalation_required is True
    assert d.confidence < 0.6 or d.reason in {
        "no_rule_match",
        "low_confidence",
        "ack_only",
    }


def test_from_any_and_duck_typed_inbound():
    """Accept sibling poller shape (weave.inbound.InboundMessage-like)."""
    from dataclasses import dataclass, field
    from typing import Any

    @dataclass
    class FakeInbound:
        thread_id: str
        message_id: str
        timestamp: str
        direction: str
        participant_phone: str | None
        participant_name: str | None
        person_id: str | None
        body_preview: str
        body: str | None = None
        raw_refs: dict[str, Any] = field(default_factory=dict)

    fake = FakeInbound(
        thread_id="thr-1",
        message_id="sms-9",
        timestamp="2026-07-28T00:00:00Z",
        direction="inbound",
        participant_phone="+12125550199",
        participant_name="Jane Doe",
        person_id="p1",
        body_preview="need portal link",
        body="Hi, can you resend the portal link?",
    )
    norm = NormalizedInboundMessage.from_any(fake)
    assert norm.message_id == "sms-9"
    assert norm.person_phone_last4 == "0199"
    d = classify_inbound(fake)
    assert d.route_key == ROUTE_ZOCDOC_NP
    log = decision_log_dict(d, norm)
    assert "Jane" not in json.dumps(log)
    assert "555" not in json.dumps(log)

