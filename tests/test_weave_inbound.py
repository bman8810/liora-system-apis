"""Unit tests for Weave inbound poll/search module (mocked WeaveClient)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from liora_tools.weave.inbound import (
    default_topic_queries,
    hydrate_threads,
    list_inbound_fallback,
    load_fixture,
    mask_phone,
    normalize_list_thread,
    normalize_search_thread,
    normalize_thread_detail,
    poll_inbound,
    preview_body,
    search_inbound,
)

FIXTURES = Path(__file__).parent / "fixtures" / "weave_inbound"


def test_mask_phone_and_preview():
    assert mask_phone("+13302067819") == "***7819"
    assert mask_phone(None) is None
    assert preview_body("hello world", max_chars=5) == "hell…"
    assert "appointment" in default_topic_queries()


def test_normalize_search_thread_snippet():
    thread = {
        "threadId": "t1",
        "personPhone": "+13302067819",
        "personId": "p1",
        "person": {"firstName": "A", "lastName": "B"},
        "messages": [
            {"smsId": "m1", "timestamp": "2026-07-27T15:00:00Z", "fragment": "need refill"}
        ],
        "resultType": "RESULT_TYPE_THREAD",
    }
    msgs = normalize_search_thread(thread, query="refill")
    assert len(msgs) == 1
    m = msgs[0]
    assert m.thread_id == "t1"
    assert m.message_id == "m1"
    assert m.participant_name == "A B"
    assert "refill" in m.body_preview
    assert m.source == "search"
    assert m.direction == "unknown"  # search snippets usually omit direction
    safe = m.to_safe_dict()
    assert safe["participant_phone"] == "***7819"
    assert "13302067819" not in str(safe)


def test_normalize_list_thread_inbound_only():
    thread = {
        "id": "t2",
        "person": {"personId": "p2", "firstName": "X", "lastName": "Y"},
        "messages": [
            {
                "id": "out1",
                "direction": "DIRECTION_OUTBOUND",
                "body": "office",
                "personPhone": "+13302067819",
                "createdAt": "2026-07-27T10:00:00Z",
            },
            {
                "id": "in1",
                "direction": "DIRECTION_INBOUND",
                "body": "patient says hi",
                "personPhone": "+13302067819",
                "createdAt": "2026-07-27T11:00:00Z",
            },
        ],
    }
    msgs = normalize_list_thread(thread, inbound_only=True)
    assert [m.message_id for m in msgs] == ["in1"]
    assert msgs[0].direction == "inbound"
    assert msgs[0].source == "list_threads"


def test_normalize_thread_detail():
    payload = {
        "thread": {
            "id": "t3",
            "personPhone": "+19175551010",
            "person": {"personId": "p3", "firstName": "Sam", "lastName": "P"},
            "items": [
                {
                    "smsMessage": {
                        "id": "m-in",
                        "direction": "DIRECTION_INBOUND",
                        "body": "reschedule please",
                        "createdAt": "2026-07-27T12:00:00Z",
                    }
                }
            ],
        }
    }
    msgs = normalize_thread_detail(payload, inbound_only=True)
    assert len(msgs) == 1
    assert msgs[0].message_id == "m-in"
    assert msgs[0].source == "thread"
    assert "reschedule" in (msgs[0].body or "")


def test_fixture_search_sample_no_network():
    msgs = load_fixture(FIXTURES / "search_sample.json")
    assert len(msgs) == 4
    assert all(m.source == "fixture" for m in msgs)
    ids = {m.message_id for m in msgs}
    assert "sms-np-reply-1" in ids
    assert "sms-refill-1" in ids


def test_poll_inbound_fixture_mode():
    result = poll_inbound(fixture_path=FIXTURES / "normalized_sample.json", inbound_only=True)
    assert result.source == "fixture"
    # outbound filtered when inbound_only (unknown kept; outbound dropped)
    assert len(result.messages) == 1
    assert result.messages[0].message_id == "sms-in-10"
    assert result.messages[0].is_inbound
    summary = result.to_safe_summary()
    assert summary["inbound_count"] == 1
    assert "13302067819" not in str(summary)


def test_poll_inbound_fixture_data_dict():
    result = poll_inbound(
        fixture_data={
            "messages": [
                {
                    "thread_id": "t",
                    "message_id": "m",
                    "timestamp": "2026-07-27T00:00:00Z",
                    "direction": "inbound",
                    "body_preview": "hi",
                }
            ]
        }
    )
    assert len(result.messages) == 1
    assert result.messages[0].body_preview == "hi"


def test_search_inbound_paginates_with_mock():
    client = MagicMock()
    client.search_messages.side_effect = [
        {
            "threads": [
                {
                    "threadId": "t-a",
                    "personPhone": "+13302067819",
                    "person": {"firstName": "A", "lastName": "A"},
                    "messages": [
                        {
                            "smsId": "m1",
                            "timestamp": "2026-07-27T10:00:00Z",
                            "fragment": "page1",
                        }
                    ],
                    "resultType": "RESULT_TYPE_THREAD",
                }
            ],
            "numResults": 2,
            "nextPageToken": "tok-2",
        },
        {
            "threads": [
                {
                    "threadId": "t-b",
                    "personPhone": "+19175551010",
                    "person": {"firstName": "B", "lastName": "B"},
                    "messages": [
                        {
                            "smsId": "m2",
                            "timestamp": "2026-07-27T11:00:00Z",
                            "fragment": "page2",
                        }
                    ],
                    "resultType": "RESULT_TYPE_THREAD",
                }
            ],
            "numResults": 2,
            "nextPageToken": "",
        },
    ]
    result = search_inbound(client, ["appointment"], page_size=1, max_pages=5)
    assert result.pages_fetched == 2
    assert {m.message_id for m in result.messages} == {"m1", "m2"}
    assert client.search_messages.call_count == 2
    # second call uses page_token from first response
    second_kwargs = client.search_messages.call_args_list[1]
    assert second_kwargs.kwargs.get("page_token") == "tok-2" or (
        len(second_kwargs.args) >= 1 and second_kwargs.kwargs.get("page_token") == "tok-2"
    )


def test_poll_inbound_live_uses_search_not_list_by_default():
    client = MagicMock()
    client.search_messages.return_value = {
        "threads": [
            {
                "threadId": "t1",
                "personPhone": "+13302067819",
                "person": {"firstName": "A", "lastName": "B"},
                "messages": [
                    {
                        "smsId": "s1",
                        "timestamp": "2026-07-27T09:00:00Z",
                        "fragment": "portal help",
                    }
                ],
                "resultType": "RESULT_TYPE_THREAD",
            }
        ],
        "numResults": 1,
        "nextPageToken": "",
    }
    result = poll_inbound(client, queries=["portal"])
    assert result.source == "search"
    assert len(result.messages) == 1
    client.list_threads.assert_not_called()
    client.search_messages.assert_called()


def test_list_fallback_only_when_opted_in_and_empty_search():
    client = MagicMock()
    client.search_messages.return_value = {
        "threads": [],
        "numResults": 0,
        "nextPageToken": "",
    }
    client.list_threads.return_value = {
        "threads": [
            {
                "id": "lt1",
                "person": {"personId": "p", "firstName": "Z", "lastName": "Z"},
                "messages": [
                    {
                        "id": "in-lt",
                        "direction": "DIRECTION_INBOUND",
                        "body": "from list",
                        "personPhone": "+13302067819",
                        "createdAt": "2026-07-27T08:00:00Z",
                    }
                ],
            }
        ],
        "olderPageToken": "",
    }
    # Without fallback flag → empty
    empty = poll_inbound(client, queries=["zzz-no-hit"], allow_list_threads_fallback=False)
    assert empty.messages == []
    client.list_threads.assert_not_called()

    hit = poll_inbound(client, queries=["zzz-no-hit"], allow_list_threads_fallback=True)
    assert hit.source == "list_threads_fallback"
    assert [m.message_id for m in hit.messages] == ["in-lt"]
    client.list_threads.assert_called()


def test_list_inbound_fallback_direct():
    client = MagicMock()
    client.list_threads.return_value = {"threads": [], "olderPageToken": ""}
    r = list_inbound_fallback(client)
    assert r.source == "list_threads"
    assert r.messages == []


def test_hydrate_threads():
    client = MagicMock()
    client.get_thread.return_value = {
        "thread": {
            "id": "thr-h",
            "personPhone": "+13302067819",
            "person": {"personId": "ph", "firstName": "C", "lastName": "K"},
            "items": [
                {
                    "smsMessage": {
                        "id": "full-in",
                        "body": "I finished the portal registration.",
                        "direction": "DIRECTION_INBOUND",
                        "createdAt": "2026-07-25T10:30:00Z",
                    }
                }
            ],
        }
    }
    from liora_tools.weave.inbound import InboundMessage

    seed = [
        InboundMessage(
            thread_id="thr-h",
            message_id="snippet",
            timestamp="2026-07-25T10:30:00Z",
            direction="unknown",
            participant_phone="+13302067819",
            participant_name="C K",
            person_id="ph",
            body_preview="finished…",
            source="search",
        )
    ]
    out = hydrate_threads(client, seed, inbound_only=True)
    assert len(out) == 1
    assert out[0].message_id == "full-in"
    assert out[0].direction == "inbound"
    client.get_thread.assert_called_once_with("thr-h")


def test_poll_inbound_requires_client_without_fixture():
    with pytest.raises(ValueError, match="client is required"):
        poll_inbound(queries=["x"])


def test_fixture_thread_detail_file():
    msgs = load_fixture(FIXTURES / "thread_detail_sample.json")
    assert any(m.message_id == "sms-h-in" for m in msgs)
    assert any(m.direction == "inbound" for m in msgs)


def test_filter_since_on_poll():
    result = poll_inbound(
        fixture_path=FIXTURES / "search_sample.json",
        since_iso="2026-07-27T17:00:00Z",
    )
    # only refill 17:01 and unknown 18:00
    ids = {m.message_id for m in result.messages}
    assert "sms-refill-1" in ids
    assert "sms-unk-1" in ids
    assert "sms-np-reply-1" not in ids
