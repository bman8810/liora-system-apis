"""Shared shapes for Weave messaging worker pipeline.

The inbound poll module (sibling card) normalizes Weave search/thread payloads
into ``NormalizedInboundMessage``. The classifier only needs text + ids; phone
and names stay out of route decisions and log dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class NormalizedInboundMessage:
    """Stable internal inbound event for classify → route → outbound.

    PHI note: ``body`` is held for classification only. Never print it, never
    put it in Genies Bottle / Telegram / decision logs. Use ``body_preview``
    (already redacted) or ``decision_log_dict`` instead.
    """

    message_id: str
    thread_id: str
    body: str
    direction: str = "inbound"
    timestamp: str | None = None
    person_id: str | None = None
    # Last-4 only — full E.164 stays in Weave client layer, not here.
    person_phone_last4: str | None = None
    # Short redacted snippet for ops (may be empty). Classifier does not need it.
    body_preview: str = ""
    # Optional thread context from poller (e.g. prior Genie NP SMS fingerprint).
    prior_outbound_fingerprints: tuple[str, ...] = ()
    # Opaque raw ids for handoff (never logged wholesale).
    raw_refs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "message_id", str(self.message_id or "").strip())
        object.__setattr__(self, "thread_id", str(self.thread_id or "").strip())
        object.__setattr__(self, "body", self.body if self.body is not None else "")
        object.__setattr__(
            self,
            "direction",
            (self.direction or "inbound").strip().lower() or "inbound",
        )
        fps = self.prior_outbound_fingerprints or ()
        if not isinstance(fps, tuple):
            object.__setattr__(self, "prior_outbound_fingerprints", tuple(fps))

    @classmethod
    def from_any(
        cls,
        message: "NormalizedInboundMessage | Mapping[str, Any] | str | Any",
        *,
        prior_outbound_fingerprints: tuple[str, ...] | list[str] | None = None,
    ) -> "NormalizedInboundMessage":
        """Coerce poller shapes (incl. ``weave.inbound.InboundMessage``) into this type.

        Accepts this class, a mapping, a raw body string, or any object with
        ``body`` / ``message_id`` / ``thread_id`` attributes (duck-typed).
        """
        extra_fps = tuple(prior_outbound_fingerprints or ())

        if isinstance(message, cls):
            if not extra_fps:
                return message
            merged = tuple(message.prior_outbound_fingerprints or ()) + extra_fps
            return cls(
                message_id=message.message_id,
                thread_id=message.thread_id,
                body=message.body,
                direction=message.direction,
                timestamp=message.timestamp,
                person_id=message.person_id,
                person_phone_last4=message.person_phone_last4,
                body_preview=message.body_preview,
                prior_outbound_fingerprints=merged,
                raw_refs=dict(message.raw_refs or {}),
            )

        if isinstance(message, str):
            return cls(
                message_id="",
                thread_id="",
                body=message,
                prior_outbound_fingerprints=extra_fps,
            )

        if isinstance(message, Mapping):
            body = message.get("body")
            if body is None:
                body = message.get("text") or message.get("body_preview") or ""
            phone = message.get("person_phone_last4")
            if phone is None:
                phone = _last4(
                    message.get("participant_phone") or message.get("person_phone")
                )
            fps = message.get("prior_outbound_fingerprints") or ()
            return cls(
                message_id=str(
                    message.get("message_id") or message.get("id") or ""
                ),
                thread_id=str(message.get("thread_id") or ""),
                body=str(body or ""),
                direction=str(message.get("direction") or "inbound"),
                timestamp=message.get("timestamp"),
                person_id=(
                    str(message["person_id"])
                    if message.get("person_id") is not None
                    else None
                ),
                person_phone_last4=phone,
                body_preview=str(message.get("body_preview") or ""),
                prior_outbound_fingerprints=tuple(fps) + extra_fps,
                raw_refs=dict(message.get("raw_refs") or {}),
            )

        # Duck-type: weave.inbound.InboundMessage and similar
        body = getattr(message, "body", None)
        if body is None:
            body = getattr(message, "body_preview", None) or getattr(
                message, "text", ""
            )
        phone = getattr(message, "person_phone_last4", None)
        if phone is None:
            phone = _last4(
                getattr(message, "participant_phone", None)
                or getattr(message, "person_phone", None)
            )
        fps = getattr(message, "prior_outbound_fingerprints", ()) or ()
        raw_refs = getattr(message, "raw_refs", None) or {}
        return cls(
            message_id=str(
                getattr(message, "message_id", None)
                or getattr(message, "id", "")
                or ""
            ),
            thread_id=str(getattr(message, "thread_id", "") or ""),
            body=str(body or ""),
            direction=str(getattr(message, "direction", None) or "inbound"),
            timestamp=getattr(message, "timestamp", None),
            person_id=(
                str(getattr(message, "person_id"))
                if getattr(message, "person_id", None) is not None
                else None
            ),
            person_phone_last4=phone,
            body_preview=str(getattr(message, "body_preview", "") or ""),
            prior_outbound_fingerprints=tuple(fps) + extra_fps,
            raw_refs=dict(raw_refs) if isinstance(raw_refs, Mapping) else {},
        )


def _last4(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(c for c in str(phone) if c.isdigit())
    if len(digits) < 4:
        return None
    return digits[-4:]
