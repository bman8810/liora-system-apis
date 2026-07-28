"""Weave inbound poll/search — durable fetch + normalize for messaging worker.

Primary path is ``search_messages`` (search index, paginated via nextPageToken).
``list_threads`` is an explicit opt-in fallback only: Firestore-backed and
practically capped (~100 recent threads), so it misses older inbox history and
must not be the default durable poller.

Auth: inject a ``WeaveClient`` built via session_manager / Kernel JWT refresh
(``get_client(\"weave\")`` or ``WeaveClient.connect()``). This module never
sends SMS.

Fixture/replay: ``poll_inbound(fixture_path=...)`` or ``load_fixture`` emits
normalized events with zero network I/O.

Normalized shape is intentionally stable for downstream classify/route
(sibling cards). Prefer ``to_safe_dict()`` for logs — never dump full bodies
or raw phone numbers to Telegram.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

# Default body preview for classify without logging full PHI blobs
DEFAULT_PREVIEW_CHARS = 280
# Max pages per search query (safety against runaway pagination)
DEFAULT_MAX_PAGES = 10

_DIRECTION_IN = re.compile(r"INBOUND", re.I)
_DIRECTION_OUT = re.compile(r"OUTBOUND", re.I)


class WeaveMessagingClient(Protocol):
    """Minimal surface used by the inbound poller (mock-friendly)."""

    def search_messages(
        self,
        query: str,
        page_size: int = 25,
        page_token: str | None = None,
    ) -> dict: ...

    def get_thread(self, thread_id: str, page_size: int = 25) -> dict: ...

    def list_threads(
        self,
        page_size: int = 25,
        page_token: str | None = None,
    ) -> dict: ...


@dataclass(frozen=True)
class InboundMessage:
    """Stable internal shape for inbound Weave SMS (classify/route input)."""

    thread_id: str
    message_id: str
    timestamp: str
    direction: str  # inbound | outbound | unknown
    participant_phone: str | None
    participant_name: str | None
    person_id: str | None
    body_preview: str
    body: str | None = None
    result_type: str | None = None
    source: str = "search"  # search | thread | list_threads | fixture
    location_id: str | None = None
    raw_refs: dict[str, Any] = field(default_factory=dict)

    @property
    def is_inbound(self) -> bool:
        return self.direction == "inbound"

    def to_dict(self, *, include_body: bool = True) -> dict[str, Any]:
        d = asdict(self)
        if not include_body:
            d["body"] = None
        return d

    def to_safe_dict(self) -> dict[str, Any]:
        """Log/Telegram-safe projection — no full body, masked phone."""
        return {
            "thread_id": self.thread_id,
            "message_id": self.message_id,
            "timestamp": self.timestamp,
            "direction": self.direction,
            "participant_phone": mask_phone(self.participant_phone),
            "participant_name": _initials(self.participant_name),
            "person_id": self.person_id,
            "body_preview_len": len(self.body_preview or ""),
            "body_preview": preview_body(self.body_preview, max_chars=80),
            "has_full_body": bool(self.body),
            "result_type": self.result_type,
            "source": self.source,
            "location_id": self.location_id,
            "raw_refs": {
                k: self.raw_refs.get(k)
                for k in ("sms_id", "thread_id", "person_id", "query", "fixture")
                if k in (self.raw_refs or {})
            },
        }


@dataclass
class InboundPollResult:
    """Result of one poll/search/fixture pass."""

    messages: list[InboundMessage]
    queries_run: list[str] = field(default_factory=list)
    next_cursors: dict[str, str] = field(default_factory=dict)
    source: str = "search"
    pages_fetched: int = 0
    raw_hit_count: int = 0

    def inbound_only(self) -> list[InboundMessage]:
        return [m for m in self.messages if m.is_inbound]

    def to_safe_summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "message_count": len(self.messages),
            "inbound_count": len(self.inbound_only()),
            "queries_run": list(self.queries_run),
            "next_cursors_keys": sorted(self.next_cursors.keys()),
            "pages_fetched": self.pages_fetched,
            "raw_hit_count": self.raw_hit_count,
            "messages": [m.to_safe_dict() for m in self.messages],
        }


# ── PHI-safe helpers ─────────────────────────────────────────────────────────


def mask_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 4:
        return "***"
    return f"***{digits[-4:]}"


def preview_body(text: str | None, max_chars: int = DEFAULT_PREVIEW_CHARS) -> str:
    if not text:
        return ""
    t = " ".join(str(text).split())
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 1] + "…"


def _initials(name: str | None) -> str | None:
    if not name or not str(name).strip():
        return None
    parts = str(name).strip().split()
    return "".join(p[0].upper() for p in parts if p)[:4]


def normalize_direction(raw: str | None) -> str:
    if not raw:
        return "unknown"
    s = str(raw)
    if _DIRECTION_IN.search(s):
        return "inbound"
    if _DIRECTION_OUT.search(s):
        return "outbound"
    return "unknown"


def _person_name(person: dict | None) -> str | None:
    if not isinstance(person, dict):
        return None
    first = (person.get("firstName") or person.get("preferredName") or "").strip()
    last = (person.get("lastName") or "").strip()
    name = f"{first} {last}".strip()
    return name or None


def _parse_ts(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, (int, float)):
        # epoch seconds or ms
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = str(value).strip()
    return s


def _stable_message_id(*parts: str | None) -> str:
    cleaned = [p for p in parts if p]
    if cleaned:
        return cleaned[0] or ""
    return ""


# ── Normalizers ──────────────────────────────────────────────────────────────


def normalize_search_thread(
    thread: dict,
    *,
    query: str | None = None,
    inbound_only: bool = False,
) -> list[InboundMessage]:
    """Normalize one search_messages thread hit into zero or more events.

    Search returns at most a small set of message snippets per thread (often 1).
    Direction is often absent on search snippets — treat as unknown unless the
    payload includes direction; callers may hydrate via get_thread.
    """
    if not isinstance(thread, dict):
        return []

    thread_id = str(
        thread.get("threadId") or thread.get("id") or thread.get("thread_id") or ""
    )
    _person_raw = thread.get("person")
    person: dict[str, Any] = _person_raw if isinstance(_person_raw, dict) else {}
    person_id = (
        thread.get("personId")
        or person.get("personId")
        or person.get("id")
        or None
    )
    if person_id is not None:
        person_id = str(person_id)
    phone = thread.get("personPhone") or thread.get("phone")
    if isinstance(phone, dict):
        phone = phone.get("number") or phone.get("phoneNumber")
    name = _person_name(person)
    result_type = thread.get("resultType") or thread.get("result_type")
    location_id = thread.get("locationId")
    messages = thread.get("messages") or []
    if not isinstance(messages, list):
        messages = []

    out: list[InboundMessage] = []
    if not messages:
        # Person-only hit with no snippet — still emit a shell event for route
        msg = InboundMessage(
            thread_id=thread_id,
            message_id=f"search-empty-{thread_id}" if thread_id else "search-empty",
            timestamp="",
            direction="unknown",
            participant_phone=str(phone) if phone else None,
            participant_name=name,
            person_id=person_id,
            body_preview="",
            body=None,
            result_type=str(result_type) if result_type else None,
            source="search",
            location_id=str(location_id) if location_id else None,
            raw_refs={
                "thread_id": thread_id,
                "person_id": person_id,
                "query": query,
            },
        )
        # Search often omits direction — keep shell for hydrate/classify.
        out.append(msg)
        return out

    for m in messages:
        if not isinstance(m, dict):
            continue
        sms_id = str(
            m.get("smsId")
            or m.get("id")
            or m.get("messageId")
            or m.get("message_id")
            or ""
        )
        body = m.get("fragment") or m.get("body") or m.get("text") or ""
        direction = normalize_direction(m.get("direction"))
        ts = _parse_ts(m.get("timestamp") or m.get("createdAt") or m.get("created_at"))
        # Keep unknown direction (search snippets); drop clear outbound when filtering.
        if inbound_only and direction == "outbound":
            continue
        out.append(
            InboundMessage(
                thread_id=thread_id,
                message_id=sms_id or _stable_message_id(thread_id, ts, preview_body(body, 40)),
                timestamp=ts,
                direction=direction,
                participant_phone=str(phone) if phone else None,
                participant_name=name,
                person_id=person_id,
                body_preview=preview_body(body),
                body=str(body) if body else None,
                result_type=str(result_type) if result_type else None,
                source="search",
                location_id=str(location_id) if location_id else None,
                raw_refs={
                    "sms_id": sms_id or None,
                    "thread_id": thread_id,
                    "person_id": person_id,
                    "query": query,
                },
            )
        )
    return out


def normalize_list_thread(
    thread: dict,
    *,
    inbound_only: bool = True,
) -> list[InboundMessage]:
    """Normalize a list_threads inbox row (messages often include direction)."""
    if not isinstance(thread, dict):
        return []
    thread_id = str(thread.get("id") or thread.get("threadId") or "")
    _person_raw = thread.get("person")
    person: dict[str, Any] = _person_raw if isinstance(_person_raw, dict) else {}
    person_id = person.get("personId") or person.get("id")
    if person_id is not None:
        person_id = str(person_id)
    name = _person_name(person)
    location_id = thread.get("locationId")
    messages = thread.get("messages") or []
    if not isinstance(messages, list):
        messages = []

    out: list[InboundMessage] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        direction = normalize_direction(m.get("direction"))
        if inbound_only and direction != "inbound":
            continue
        phone = m.get("personPhone") or thread.get("personPhone")
        body = m.get("body") or ""
        sms_id = str(m.get("id") or m.get("smsId") or "")
        ts = _parse_ts(m.get("createdAt") or m.get("timestamp"))
        out.append(
            InboundMessage(
                thread_id=thread_id,
                message_id=sms_id or _stable_message_id(thread_id, ts),
                timestamp=ts,
                direction=direction,
                participant_phone=str(phone) if phone else None,
                participant_name=name,
                person_id=person_id,
                body_preview=preview_body(body),
                body=str(body) if body else None,
                result_type=None,
                source="list_threads",
                location_id=str(location_id) if location_id else None,
                raw_refs={
                    "sms_id": sms_id or None,
                    "thread_id": thread_id,
                    "person_id": person_id,
                    "thread_status": thread.get("status"),
                },
            )
        )
    return out


def normalize_thread_detail(
    payload: dict,
    *,
    inbound_only: bool = True,
) -> list[InboundMessage]:
    """Normalize get_thread unified payload into message events."""
    if not isinstance(payload, dict):
        return []
    thread = payload.get("thread") if isinstance(payload.get("thread"), dict) else payload
    if not isinstance(thread, dict):
        return []

    thread_id = str(thread.get("id") or thread.get("threadId") or "")
    _person_raw = thread.get("person")
    person: dict[str, Any] = _person_raw if isinstance(_person_raw, dict) else {}
    person_id = person.get("personId") or person.get("id") or thread.get("personId")
    if person_id is not None:
        person_id = str(person_id)
    name = _person_name(person)
    location_id = thread.get("locationId")
    person_phone = thread.get("personPhone")

    items = thread.get("items")
    messages_raw: list[dict] = []
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            sms = it.get("smsMessage") or it.get("message") or it
            if isinstance(sms, dict) and (sms.get("body") is not None or sms.get("id")):
                messages_raw.append(sms)
    elif isinstance(thread.get("messages"), list):
        messages_raw = [m for m in thread["messages"] if isinstance(m, dict)]

    out: list[InboundMessage] = []
    for m in messages_raw:
        direction = normalize_direction(m.get("direction"))
        if inbound_only and direction != "inbound":
            continue
        phone = m.get("personPhone") or person_phone
        body = m.get("body") or ""
        sms_id = str(m.get("id") or m.get("smsId") or "")
        ts = _parse_ts(m.get("createdAt") or m.get("timestamp"))
        out.append(
            InboundMessage(
                thread_id=thread_id,
                message_id=sms_id or _stable_message_id(thread_id, ts),
                timestamp=ts,
                direction=direction,
                participant_phone=str(phone) if phone else None,
                participant_name=name,
                person_id=person_id,
                body_preview=preview_body(body),
                body=str(body) if body else None,
                result_type=None,
                source="thread",
                location_id=str(location_id) if location_id else None,
                raw_refs={
                    "sms_id": sms_id or None,
                    "thread_id": thread_id,
                    "person_id": person_id,
                },
            )
        )
    return out


def dedupe_messages(messages: Iterable[InboundMessage]) -> list[InboundMessage]:
    """Dedupe by (thread_id, message_id), keeping first occurrence."""
    seen: set[tuple[str, str]] = set()
    out: list[InboundMessage] = []
    for m in messages:
        key = (m.thread_id, m.message_id)
        if key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def filter_since(
    messages: Iterable[InboundMessage],
    since_iso: str | None,
) -> list[InboundMessage]:
    """Drop messages with timestamp strictly before since_iso (when both parse)."""
    if not since_iso:
        return list(messages)
    since_dt = _try_parse_dt(since_iso)
    if since_dt is None:
        return list(messages)
    out: list[InboundMessage] = []
    for m in messages:
        if not m.timestamp:
            out.append(m)  # keep unknown ts for safety (downstream can filter)
            continue
        mt = _try_parse_dt(m.timestamp)
        if mt is None or mt >= since_dt:
            out.append(m)
    return out


def _try_parse_dt(value: str) -> datetime | None:
    s = value.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# ── Fixture / replay ─────────────────────────────────────────────────────────


def load_fixture(path: str | Path) -> list[InboundMessage]:
    """Load canned search/list/thread payloads or pre-normalized events.

    Accepted JSON shapes:
      - ``{\"messages\": [InboundMessage-like dicts...]}``
      - ``{\"threads\": [...]}`` search or list style
      - bare list of message dicts or thread dicts
      - ``{\"thread\": {...}}`` get_thread style
    """
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    return normalize_fixture_payload(data, fixture_name=p.name)


def normalize_fixture_payload(
    data: Any,
    *,
    fixture_name: str = "fixture",
) -> list[InboundMessage]:
    if isinstance(data, list):
        # list of messages or threads
        if data and isinstance(data[0], dict) and (
            "message_id" in data[0] or "body_preview" in data[0]
        ):
            return [_message_from_dict(d, source="fixture") for d in data]
        msgs: list[InboundMessage] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if "messages" in item and ("threadId" in item or "id" in item):
                if "threadId" in item or item.get("resultType"):
                    msgs.extend(normalize_search_thread(item, query="fixture"))
                else:
                    msgs.extend(normalize_list_thread(item, inbound_only=False))
            elif "body_preview" in item or "message_id" in item:
                msgs.append(_message_from_dict(item, source="fixture"))
            else:
                msgs.extend(normalize_search_thread(item, query="fixture"))
                if not any(m.thread_id == str(item.get("id") or "") for m in msgs[-3:]):
                    msgs.extend(normalize_list_thread(item, inbound_only=False))
        return _tag_fixture(dedupe_messages(msgs), fixture_name)

    if not isinstance(data, dict):
        raise ValueError(f"Unsupported fixture type: {type(data)}")

    if "messages" in data and isinstance(data["messages"], list):
        sample = data["messages"][0] if data["messages"] else {}
        if isinstance(sample, dict) and (
            "message_id" in sample or "body_preview" in sample or "direction" in sample
            and "thread_id" in sample
        ):
            return [
                _message_from_dict(m, source="fixture")
                for m in data["messages"]
                if isinstance(m, dict)
            ]

    if "thread" in data or (data.get("items") and data.get("id")):
        msgs = normalize_thread_detail(data, inbound_only=False)
        return _tag_fixture(msgs, fixture_name)

    threads = data.get("threads")
    if isinstance(threads, list):
        msgs = []
        for t in threads:
            if not isinstance(t, dict):
                continue
            # Heuristic: search hits use threadId + resultType; list uses id + status
            if t.get("threadId") or t.get("resultType") or (
                "messages" in t and t.get("messages") and isinstance(t["messages"][0], dict)
                and ("fragment" in t["messages"][0] or "smsId" in t["messages"][0])
            ):
                msgs.extend(normalize_search_thread(t, query="fixture"))
            else:
                msgs.extend(normalize_list_thread(t, inbound_only=False))
        return _tag_fixture(dedupe_messages(msgs), fixture_name)

    raise ValueError(
        f"Fixture {fixture_name!r} missing messages/threads/thread keys"
    )


def _tag_fixture(messages: list[InboundMessage], fixture_name: str) -> list[InboundMessage]:
    tagged: list[InboundMessage] = []
    for m in messages:
        refs = dict(m.raw_refs or {})
        refs["fixture"] = fixture_name
        tagged.append(
            InboundMessage(
                thread_id=m.thread_id,
                message_id=m.message_id,
                timestamp=m.timestamp,
                direction=m.direction,
                participant_phone=m.participant_phone,
                participant_name=m.participant_name,
                person_id=m.person_id,
                body_preview=m.body_preview,
                body=m.body,
                result_type=m.result_type,
                source="fixture",
                location_id=m.location_id,
                raw_refs=refs,
            )
        )
    return tagged


def _message_from_dict(d: dict, *, source: str = "fixture") -> InboundMessage:
    raw_dir = d.get("direction")
    # Already-normalized values pass through; Weave enums go through helper.
    if raw_dir in ("inbound", "outbound", "unknown"):
        direction = str(raw_dir)
    else:
        direction = normalize_direction(raw_dir)
    pid = d.get("person_id", d.get("personId"))
    return InboundMessage(
        thread_id=str(d.get("thread_id") or d.get("threadId") or ""),
        message_id=str(d.get("message_id") or d.get("messageId") or d.get("id") or ""),
        timestamp=str(d.get("timestamp") or d.get("createdAt") or ""),
        direction=direction,
        participant_phone=d.get("participant_phone") or d.get("personPhone"),
        participant_name=d.get("participant_name") or d.get("participantName"),
        person_id=str(pid) if pid is not None else None,
        body_preview=preview_body(d.get("body_preview") or d.get("body") or ""),
        body=d.get("body"),
        result_type=d.get("result_type") or d.get("resultType"),
        source=source,
        location_id=d.get("location_id") or d.get("locationId"),
        raw_refs=dict(d.get("raw_refs") or d.get("rawRefs") or {}),
    )


# ── Live poll / search ───────────────────────────────────────────────────────


def search_inbound(
    client: WeaveMessagingClient,
    queries: Iterable[str],
    *,
    page_size: int = 25,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_tokens: dict[str, str] | None = None,
    inbound_only: bool = False,
    since_iso: str | None = None,
) -> InboundPollResult:
    """Primary durable path: multi-query ``search_messages`` with cursors.

    ``page_tokens`` maps query → prior nextPageToken for incremental resumes.
    Returned ``next_cursors`` carries tokens for the next poll.
    """
    queries_run: list[str] = []
    next_cursors: dict[str, str] = {}
    all_msgs: list[InboundMessage] = []
    pages = 0
    raw_hits = 0
    tokens_in = dict(page_tokens or {})

    for q in queries:
        q = (q or "").strip()
        if not q:
            continue
        queries_run.append(q)
        token: str | None = tokens_in.get(q) or None
        pages_this = 0
        last_next = ""
        while pages_this < max_pages:
            resp = client.search_messages(q, page_size=page_size, page_token=token)
            pages += 1
            pages_this += 1
            threads = resp.get("threads") or []
            if not isinstance(threads, list):
                threads = []
            raw_hits += len(threads)
            for t in threads:
                if isinstance(t, dict):
                    all_msgs.extend(
                        normalize_search_thread(
                            t, query=q, inbound_only=inbound_only
                        )
                    )
            last_next = (resp.get("nextPageToken") or "").strip()
            if not last_next:
                break
            token = last_next
        if last_next:
            next_cursors[q] = last_next

    msgs = dedupe_messages(all_msgs)
    msgs = filter_since(msgs, since_iso)
    return InboundPollResult(
        messages=msgs,
        queries_run=queries_run,
        next_cursors=next_cursors,
        source="search",
        pages_fetched=pages,
        raw_hit_count=raw_hits,
    )


def list_inbound_fallback(
    client: WeaveMessagingClient,
    *,
    page_size: int = 25,
    max_pages: int = 3,
    page_token: str | None = None,
    inbound_only: bool = True,
    since_iso: str | None = None,
) -> InboundPollResult:
    """Explicit fallback via list_threads (capped). Do not use as primary path.

    Why secondary: Weave ``list_threads`` is Firestore-backed and effectively
    limited to ~recent 100 threads. Older inbound SMS never appears. Prefer
    targeted ``search_messages`` queries for durable coverage.
    """
    all_msgs: list[InboundMessage] = []
    pages = 0
    raw_hits = 0
    token = page_token
    next_tok = ""
    for _ in range(max_pages):
        resp = client.list_threads(page_size=page_size, page_token=token)
        pages += 1
        threads = resp.get("threads") or []
        if not isinstance(threads, list):
            threads = []
        raw_hits += len(threads)
        for t in threads:
            if isinstance(t, dict):
                all_msgs.extend(
                    normalize_list_thread(t, inbound_only=inbound_only)
                )
        next_tok = (resp.get("olderPageToken") or "").strip()
        if not next_tok:
            break
        token = next_tok

    msgs = dedupe_messages(all_msgs)
    msgs = filter_since(msgs, since_iso)
    cursors = {"list_threads": next_tok} if next_tok else {}
    return InboundPollResult(
        messages=msgs,
        queries_run=[],
        next_cursors=cursors,
        source="list_threads",
        pages_fetched=pages,
        raw_hit_count=raw_hits,
    )


def hydrate_threads(
    client: WeaveMessagingClient,
    messages: Iterable[InboundMessage],
    *,
    inbound_only: bool = True,
    max_threads: int = 25,
) -> list[InboundMessage]:
    """Expand search snippets via get_thread for full inbound bodies."""
    by_thread: dict[str, list[InboundMessage]] = {}
    for m in messages:
        if not m.thread_id:
            continue
        by_thread.setdefault(m.thread_id, []).append(m)

    out: list[InboundMessage] = []
    for i, tid in enumerate(by_thread):
        if i >= max_threads:
            # keep unhydrated leftovers
            for rest_tid in list(by_thread.keys())[i:]:
                out.extend(by_thread[rest_tid])
            break
        detail = client.get_thread(tid)
        hydrated = normalize_thread_detail(detail, inbound_only=inbound_only)
        if hydrated:
            out.extend(hydrated)
        else:
            out.extend(by_thread[tid])
    # messages without thread_id
    for m in messages:
        if not m.thread_id:
            out.append(m)
    return dedupe_messages(out)


def poll_inbound(
    client: WeaveMessagingClient | None = None,
    *,
    queries: Iterable[str] | None = None,
    fixture_path: str | Path | None = None,
    fixture_data: Any | None = None,
    page_size: int = 25,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_tokens: dict[str, str] | None = None,
    allow_list_threads_fallback: bool = False,
    hydrate: bool = False,
    inbound_only: bool = False,
    since_iso: str | None = None,
) -> InboundPollResult:
    """Unified entry: fixture/replay OR live search (optional list fallback).

    - Fixture mode: no client required; never hits network.
    - Live mode: requires client; primary = search_messages over ``queries``.
    - ``allow_list_threads_fallback=True`` only when queries empty or search
      yields nothing *and* operator opts in (documented secondary path).
    - Does not send SMS. Does not enable cron.
    """
    if fixture_path is not None:
        msgs = load_fixture(fixture_path)
        msgs = filter_since(msgs, since_iso)
        if inbound_only:
            msgs = [m for m in msgs if m.is_inbound or m.direction == "unknown"]
        return InboundPollResult(
            messages=dedupe_messages(msgs),
            queries_run=[],
            next_cursors={},
            source="fixture",
            pages_fetched=0,
            raw_hit_count=len(msgs),
        )

    if fixture_data is not None:
        msgs = normalize_fixture_payload(fixture_data)
        msgs = filter_since(msgs, since_iso)
        if inbound_only:
            msgs = [m for m in msgs if m.is_inbound or m.direction == "unknown"]
        return InboundPollResult(
            messages=dedupe_messages(msgs),
            queries_run=[],
            next_cursors={},
            source="fixture",
            pages_fetched=0,
            raw_hit_count=len(msgs),
        )

    if client is None:
        raise ValueError(
            "client is required for live poll (or pass fixture_path/fixture_data)"
        )

    qlist = [q.strip() for q in (queries or []) if q and str(q).strip()]
    result = InboundPollResult(messages=[], source="search")

    if qlist:
        result = search_inbound(
            client,
            qlist,
            page_size=page_size,
            max_pages=max_pages,
            page_tokens=page_tokens,
            inbound_only=inbound_only,
            since_iso=since_iso,
        )

    if allow_list_threads_fallback and not result.messages:
        fb = list_inbound_fallback(
            client,
            page_size=page_size,
            max_pages=min(max_pages, 3),
            page_token=(page_tokens or {}).get("list_threads"),
            inbound_only=True if inbound_only else True,
            since_iso=since_iso,
        )
        if qlist:
            # merge: search was empty
            result = InboundPollResult(
                messages=fb.messages,
                queries_run=result.queries_run,
                next_cursors={**result.next_cursors, **fb.next_cursors},
                source="list_threads_fallback",
                pages_fetched=result.pages_fetched + fb.pages_fetched,
                raw_hit_count=result.raw_hit_count + fb.raw_hit_count,
            )
        else:
            result = fb

    if hydrate and result.messages:
        hydrated = hydrate_threads(
            client, result.messages, inbound_only=bool(inbound_only)
        )
        result = InboundPollResult(
            messages=hydrated,
            queries_run=result.queries_run,
            next_cursors=result.next_cursors,
            source=result.source + "+hydrate",
            pages_fetched=result.pages_fetched,
            raw_hit_count=result.raw_hit_count,
        )

    return result


def default_topic_queries() -> list[str]:
    """Starter query set for practice inbound sweeps (extend per flow).

    Search requires a non-empty query — there is no true \"all inbound\" search.
    Topic terms catch common patient replies; Zocdoc NP fingerprint catches
    threads already in the welcome flow. Operators should add phone last-10
    or names only when targeting known patients (PHI-sensitive).
    """
    return [
        "appointment",
        "reschedule",
        "cancel",
        "refill",
        "prescription",
        "portal",
        "insurance",
        "booking cost of $100",
    ]
