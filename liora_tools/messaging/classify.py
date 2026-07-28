"""Pure(ish) inbound message classifier / router.

Deterministic rules first. Unknown or low-confidence → escalate-to-staff.
No network I/O. No AI gate for escalation (AI must never be the sole reason
to *not* escalate).

PHI: never log raw body/name/full phone. Use ``decision_log_dict``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from liora_tools.messaging.types import NormalizedInboundMessage

# ── Route keys (extensible map) ─────────────────────────────────────────────

ROUTE_ZOCDOC_NP = "zocdoc_np"
ROUTE_SCHEDULE = "schedule"
ROUTE_REFILL = "refill"
ROUTE_ESCALATE = "escalate_to_staff"

# Handler hints consumed by worker / outbound (stable strings).
HANDLER_ZOCDOC_NP = "handler.zocdoc_np_reply"
HANDLER_SCHEDULE = "handler.schedule_question"
HANDLER_REFILL = "handler.refill_triage"
HANDLER_ESCALATE = "handler.staff_queue"

DEFAULT_ROUTES: dict[str, dict[str, Any]] = {
    ROUTE_ZOCDOC_NP: {
        "handler_hint": HANDLER_ZOCDOC_NP,
        "description": "Zocdoc new-patient registration / portal / $100 fee thread",
        "staff_escalation_default": False,
    },
    ROUTE_SCHEDULE: {
        "handler_hint": HANDLER_SCHEDULE,
        "description": "Book / reschedule / cancel / appointment timing questions",
        "staff_escalation_default": False,
    },
    ROUTE_REFILL: {
        "handler_hint": HANDLER_REFILL,
        "description": "Rx or product refill request",
        "staff_escalation_default": False,
    },
    ROUTE_ESCALATE: {
        "handler_hint": HANDLER_ESCALATE,
        "description": "Staff handoff (safe default / multi-intent / explicit human)",
        "staff_escalation_default": True,
    },
}

# Fingerprint from Genie NP template (zocdoc_new_booking.SMS_FINGERPRINT)
ZOCDOC_NP_OUTBOUND_FINGERPRINT = "booking cost of $100"

# Confidence floor: below this → force escalate even if a weak rule hit.
MIN_ROUTE_CONFIDENCE = 0.55
# When two+ non-escalate routes fire, escalate (safe multi-intent).
MULTI_INTENT_MIN_SCORE = 0.55

_WS_RE = re.compile(r"\s+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_DOB_RE = re.compile(
    r"\b(?:0?[1-9]|1[0-2])[/-](?:0?[1-9]|[12]\d|3[01])[/-](?:19|20)\d{2}\b"
)


@dataclass(frozen=True)
class RouteDecision:
    """Classifier output for the messaging worker loop."""

    route_key: str
    confidence: float
    reason: str
    handler_hint: str
    staff_escalation_required: bool
    matched_rules: tuple[str, ...] = ()
    secondary_routes: tuple[str, ...] = ()
    # Extensible bag (scores, flags) — must stay PHI-free.
    meta: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["meta"] = dict(self.meta or {})
        d["matched_rules"] = list(self.matched_rules or ())
        d["secondary_routes"] = list(self.secondary_routes or ())
        return d


@dataclass(frozen=True)
class _RuleHit:
    route_key: str
    rule_id: str
    weight: float
    reason: str


def _norm_text(text: str) -> str:
    t = (text or "").strip().lower()
    t = _WS_RE.sub(" ", t)
    return t


def _compile_any(patterns: Sequence[str]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.IGNORECASE)


# Explicit human / staff request — always wins escalate.
_ESCALATE_PATTERNS = _compile_any(
    [
        r"\b(speak|talk|call)\s+(to\s+)?(a\s+)?(human|person|someone|staff|nurse|doctor|md|provider|rep|agent)\b",
        r"\b(real|live)\s+(person|human|agent)\b",
        r"\b(transfer|connect)\s+me\b",
        r"\bcall\s+me\s+back\b",
        r"\bplease\s+call\b",
        r"\bfront\s+desk\b",
        r"\bmanager\b",
        r"\bemergency\b",
        r"\burgent\b",
        r"\bthis\s+is\s+an?\s+emergency\b",
        r"\bi\s+need\s+(to\s+)?(speak|talk)\b",
    ]
)

# Zocdoc NP — replies to Genie NP SMS / registration / portal / fee language.
_ZOCDOC_NP_PATTERNS = _compile_any(
    [
        r"\bzoc\s*doc\b",
        r"\bzocdoc\b",
        r"\bportal\s+link\b",
        r"\bresend\s+(the\s+)?portal\b",
        r"\b(need|send|resend)\s+(me\s+)?(the\s+)?portal\b",
        r"\bpatient\s+portal\b",
        r"\blog\s*in\s*to\s+(the\s+)?portal\b",
        r"\bcan'?t\s+log\s+in\b",
        r"\bcomplete\s+(my\s+)?registration\b",
        r"\bcredit\s+card\s+on\s+file\b",
        r"\bcard\s+on\s+file\b",
        r"\bbooking\s+cost\b",
        r"\$\s*100\b",
        r"\bnew\s+patient\s+(forms?|registration|paperwork)\b",
        r"\bregistration\s+(link|email|forms?)\b",
        r"\bconfirm(ing)?\s+(my\s+)?(zocdoc\s+)?appointment\b",
        r"\brelease\s+(the\s+)?appointment\b",
        # bare "portal" is NP-leaning (Genie SMS asks patients to use portal)
        r"\bportal\b",
    ]
)

_SCHEDULE_PATTERNS = _compile_any(
    [
        r"\breschedul",
        r"\bcancel(l?ation|l?ing)?\b",
        # Avoid matching NP "booking cost" — require schedule object or bare "book".
        r"\bbook\b(?!\s*ing\s+cost)",
        r"\bbooking\s+(an?\s+)?(appointment|appt|visit|time)\b",
        r"\bschedule\b",
        r"\bappointment\b",
        r"\bappt\b",
        r"\bavailable\b",
        r"\bavailability\b",
        r"\bnext\s+available\b",
        r"\bopen(ing)?s?\s+(slot|time|appointment|appt)?\b",
        r"\bmove\s+(my\s+)?(appointment|appt|visit)\b",
        r"\bchange\s+(my\s+)?(appointment|appt|time|visit)\b",
        r"\bwhat\s+time\b",
        r"\bwhen\s+is\s+my\b",
        r"\bconfirm(ing)?\s+(my\s+)?(appointment|appt|visit)\b",
        r"\bcame\s+in\b",
        r"\bsee\s+(dr\.?|doctor|provider)\b",
        r"\bfollow[\s-]?up\b",
        r"\bf/?u\b",
    ]
)

_REFILL_PATTERNS = _compile_any(
    [
        r"\brefill",
        r"\bprescription\b",
        r"\bprescri(be|bed|ption)\b",
        r"\brx\b",
        r"\bmedication\b",
        r"\bmedicine\b",
        r"\bpharmacy\b",
        r"\bran\s+out\b",
        r"\bout\s+of\s+(my\s+)?(meds?|cream|pills?|rx)\b",
        r"\btretinoin\b",
        r"\bretin[- ]?a\b",
        r"\baccutane\b",
        r"\bisotretinoin\b",
        r"\bcompound(ed)?\b",
        r"\bskin\s+care\s+(product|cream)\b",
        r"\breorder\b",
        r"\bneed\s+more\s+(of\s+)?(my\s+)?(cream|gel|ointment|meds?)\b",
    ]
)

# Soft ack / thanks alone is not a route — escalate for human glance unless
# paired with other signals (handled via empty secondary hits).
_ACK_ONLY = _compile_any(
    [
        r"^(ok|okay|thanks|thank you|thx|ty|got it|will do|sounds good|perfect|great)[.!]?$",
        r"^(yes|no|yep|nope)[.!]?$",
    ]
)


def _score_patterns(
    text: str,
    route_key: str,
    pattern: re.Pattern[str],
    rule_prefix: str,
    base_weight: float = 0.7,
) -> list[_RuleHit]:
    hits: list[_RuleHit] = []
    for m in pattern.finditer(text):
        frag = m.group(0).lower().strip()
        # Slight bump for longer / more specific fragments
        bump = min(0.2, 0.02 * max(0, len(frag) - 6))
        hits.append(
            _RuleHit(
                route_key=route_key,
                rule_id=f"{rule_prefix}:{frag[:40]}",
                weight=min(0.95, base_weight + bump),
                reason=f"matched_{route_key}_keyword",
            )
        )
    return hits


def _collect_hits(
    text: str,
    *,
    prior_fingerprints: Iterable[str] = (),
) -> list[_RuleHit]:
    hits: list[_RuleHit] = []

    if _ESCALATE_PATTERNS.search(text):
        hits.append(
            _RuleHit(
                route_key=ROUTE_ESCALATE,
                rule_id="escalate:explicit_human",
                weight=0.95,
                reason="explicit_staff_or_callback_request",
            )
        )

    hits.extend(
        _score_patterns(text, ROUTE_ZOCDOC_NP, _ZOCDOC_NP_PATTERNS, "zocdoc_np", 0.72)
    )
    hits.extend(
        _score_patterns(text, ROUTE_SCHEDULE, _SCHEDULE_PATTERNS, "schedule", 0.68)
    )
    hits.extend(
        _score_patterns(text, ROUTE_REFILL, _REFILL_PATTERNS, "refill", 0.72)
    )

    fps = {(_norm_text(f) if f else "") for f in prior_fingerprints}
    fps.discard("")
    # Thread previously got Genie NP SMS → inbound more likely NP follow-up.
    if ZOCDOC_NP_OUTBOUND_FINGERPRINT in fps or any(
        "booking cost of $100" in f for f in fps
    ):
        # Only boost when body also smells like NP/portal; weak alone is not enough
        # to route (still need patient text). Provide a mild prior for ties.
        if any(h.route_key == ROUTE_ZOCDOC_NP for h in hits) or _ZOCDOC_NP_PATTERNS.search(
            text
        ):
            hits.append(
                _RuleHit(
                    route_key=ROUTE_ZOCDOC_NP,
                    rule_id="zocdoc_np:prior_outbound_fingerprint",
                    weight=0.8,
                    reason="thread_has_np_outbound_fingerprint",
                )
            )
        elif text and not _ACK_ONLY.match(text):
            hits.append(
                _RuleHit(
                    route_key=ROUTE_ZOCDOC_NP,
                    rule_id="zocdoc_np:prior_outbound_soft",
                    weight=0.5,
                    reason="thread_has_np_outbound_soft_prior",
                )
            )

    return hits


def _best_per_route(hits: Sequence[_RuleHit]) -> dict[str, float]:
    scores: dict[str, float] = {}
    for h in hits:
        # Max weight + small stack for multiple distinct rules on same route
        prev = scores.get(h.route_key, 0.0)
        if h.weight >= prev:
            # stack mild bonus if already present
            bonus = 0.05 if prev > 0 else 0.0
            scores[h.route_key] = min(0.99, h.weight + bonus)
        else:
            scores[h.route_key] = min(0.99, prev + 0.03)
    return scores


def _pick_route(
    scores: Mapping[str, float],
    hits: Sequence[_RuleHit],
) -> RouteDecision:
    if not scores:
        return RouteDecision(
            route_key=ROUTE_ESCALATE,
            confidence=0.4,
            reason="no_rule_match",
            handler_hint=HANDLER_ESCALATE,
            staff_escalation_required=True,
            matched_rules=(),
            secondary_routes=(),
            meta={"scores": {}},
        )

    # Explicit escalate with high weight always wins.
    esc = scores.get(ROUTE_ESCALATE, 0.0)
    if esc >= 0.9:
        matched = tuple(h.rule_id for h in hits if h.route_key == ROUTE_ESCALATE)
        reason = next(
            (h.reason for h in hits if h.route_key == ROUTE_ESCALATE),
            "explicit_escalate",
        )
        return RouteDecision(
            route_key=ROUTE_ESCALATE,
            confidence=esc,
            reason=reason,
            handler_hint=HANDLER_ESCALATE,
            staff_escalation_required=True,
            matched_rules=matched,
            secondary_routes=tuple(
                r for r, s in scores.items() if r != ROUTE_ESCALATE and s >= MULTI_INTENT_MIN_SCORE
            ),
            meta={"scores": dict(scores)},
        )

    actionable = {
        k: v
        for k, v in scores.items()
        if k != ROUTE_ESCALATE and v >= MULTI_INTENT_MIN_SCORE
    }

    if len(actionable) >= 2:
        ordered = tuple(sorted(actionable, key=lambda k: (-actionable[k], k)))
        return RouteDecision(
            route_key=ROUTE_ESCALATE,
            confidence=min(0.9, max(actionable.values()) + 0.05),
            reason="multi_intent",
            handler_hint=HANDLER_ESCALATE,
            staff_escalation_required=True,
            matched_rules=tuple(h.rule_id for h in hits),
            secondary_routes=ordered,
            meta={"scores": dict(scores)},
        )

    if not actionable:
        # Only weak scores or escalate-only weak
        top_key = max(scores, key=lambda k: (scores[k], k == ROUTE_ESCALATE))
        top_score = scores[top_key]
        if top_score < MIN_ROUTE_CONFIDENCE or top_key == ROUTE_ESCALATE:
            return RouteDecision(
                route_key=ROUTE_ESCALATE,
                confidence=max(0.35, top_score if top_key == ROUTE_ESCALATE else 0.45),
                reason="low_confidence" if top_score < MIN_ROUTE_CONFIDENCE else "escalate_signal",
                handler_hint=HANDLER_ESCALATE,
                staff_escalation_required=True,
                matched_rules=tuple(h.rule_id for h in hits),
                secondary_routes=(),
                meta={"scores": dict(scores)},
            )

    # Single clear actionable route
    route_key = max(actionable, key=lambda k: (actionable[k], k))
    conf = actionable[route_key]
    if conf < MIN_ROUTE_CONFIDENCE:
        return RouteDecision(
            route_key=ROUTE_ESCALATE,
            confidence=0.45,
            reason="low_confidence",
            handler_hint=HANDLER_ESCALATE,
            staff_escalation_required=True,
            matched_rules=tuple(h.rule_id for h in hits),
            secondary_routes=(route_key,),
            meta={"scores": dict(scores)},
        )

    info = DEFAULT_ROUTES[route_key]
    matched = tuple(h.rule_id for h in hits if h.route_key == route_key)
    reason = next(
        (h.reason for h in hits if h.route_key == route_key),
        f"matched_{route_key}",
    )
    return RouteDecision(
        route_key=route_key,
        confidence=conf,
        reason=reason,
        handler_hint=str(info["handler_hint"]),
        staff_escalation_required=bool(info.get("staff_escalation_default", False)),
        matched_rules=matched,
        secondary_routes=(),
        meta={"scores": dict(scores)},
    )


def classify_inbound(
    message: NormalizedInboundMessage | Mapping[str, Any] | str | Any,
    *,
    routes: Mapping[str, Mapping[str, Any]] | None = None,
    prior_outbound_fingerprints: Sequence[str] | None = None,
) -> RouteDecision:
    """Classify a normalized inbound message into a route decision.

    Args:
        message: ``NormalizedInboundMessage``, ``weave.inbound.InboundMessage``
            (duck-typed), mapping with ``body``, or raw body str.
        routes: Optional override of route map (must include escalate_to_staff).
        prior_outbound_fingerprints: Extra thread fingerprints (merged with message).

    Returns:
        ``RouteDecision``. Unknown/empty/low-confidence → ``escalate_to_staff``.
    """
    _ = routes or DEFAULT_ROUTES  # reserved for future custom maps; keys stay stable

    norm = NormalizedInboundMessage.from_any(
        message,
        prior_outbound_fingerprints=tuple(prior_outbound_fingerprints or ()),
    )
    body = norm.body
    fps = tuple(norm.prior_outbound_fingerprints or ())
    msg_direction = (norm.direction or "inbound").lower()

    # Outbound / system noise should not auto-route to patient handlers.
    if msg_direction and msg_direction not in ("inbound", "in", "patient", ""):
        return RouteDecision(
            route_key=ROUTE_ESCALATE,
            confidence=0.3,
            reason="non_inbound_direction",
            handler_hint=HANDLER_ESCALATE,
            staff_escalation_required=True,
            matched_rules=("direction_guard",),
            secondary_routes=(),
            meta={"direction": msg_direction},
        )

    text = _norm_text(body)
    if not text:
        return RouteDecision(
            route_key=ROUTE_ESCALATE,
            confidence=0.35,
            reason="empty_body",
            handler_hint=HANDLER_ESCALATE,
            staff_escalation_required=True,
            matched_rules=("empty",),
            secondary_routes=(),
            meta={"scores": {}},
        )

    if _ACK_ONLY.match(text):
        return RouteDecision(
            route_key=ROUTE_ESCALATE,
            confidence=0.5,
            reason="ack_only",
            handler_hint=HANDLER_ESCALATE,
            staff_escalation_required=True,
            matched_rules=("ack_only",),
            secondary_routes=(),
            meta={"scores": {}},
        )

    hits = _collect_hits(text, prior_fingerprints=fps)
    scores = _best_per_route(hits)
    return _pick_route(scores, hits)


def redact_text_for_log(text: str | None, *, max_len: int = 80) -> str:
    """Redact emails/phones/DOB from a snippet. Never a substitute for omitting body."""
    if not text:
        return ""
    t = _EMAIL_RE.sub("[email]", text)
    t = _PHONE_RE.sub("[phone]", t)
    t = _DOB_RE.sub("[dob]", t)
    t = _WS_RE.sub(" ", t).strip()
    if len(t) > max_len:
        return t[: max_len - 3] + "..."
    return t


def body_fingerprint(body: str | None) -> str:
    """Short non-reversible body id for logs (not a security hash)."""
    raw = (body or "").encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def decision_log_dict(
    decision: RouteDecision,
    message: NormalizedInboundMessage | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """PHI-safe summary for logs / Telegram / GB activity.

    Includes ids, route, confidence, reason, body length + hash — never body.
    """
    out: dict[str, Any] = {
        "route_key": decision.route_key,
        "confidence": round(float(decision.confidence), 3),
        "reason": decision.reason,
        "handler_hint": decision.handler_hint,
        "staff_escalation_required": bool(decision.staff_escalation_required),
        "matched_rules": list(decision.matched_rules),
        "secondary_routes": list(decision.secondary_routes),
    }
    if decision.meta:
        # Only allow known-safe meta keys
        scores = decision.meta.get("scores")
        if isinstance(scores, dict):
            out["scores"] = {
                str(k): round(float(v), 3) for k, v in scores.items() if isinstance(v, (int, float))
            }
        direction = decision.meta.get("direction")
        if isinstance(direction, str) and direction:
            out["direction"] = direction[:32]

    if message is None:
        return out

    if isinstance(message, NormalizedInboundMessage):
        out["message_id"] = message.message_id
        out["thread_id"] = message.thread_id
        out["body_len"] = len(message.body or "")
        out["body_fp"] = body_fingerprint(message.body)
        if message.person_phone_last4:
            out["phone_last4"] = str(message.person_phone_last4)[-4:]
        if message.person_id:
            out["person_id"] = str(message.person_id)[:64]
    else:
        mid = message.get("message_id") or message.get("id")
        tid = message.get("thread_id")
        body = message.get("body") or message.get("text") or ""
        if mid is not None:
            out["message_id"] = str(mid)
        if tid is not None:
            out["thread_id"] = str(tid)
        out["body_len"] = len(str(body))
        out["body_fp"] = body_fingerprint(str(body))
        last4 = message.get("person_phone_last4")
        if last4:
            out["phone_last4"] = str(last4)[-4:]

    # Guard: never echo body/preview keys if caller stuffed them into meta
    for banned in ("body", "text", "body_preview", "name", "phone", "email", "dob"):
        out.pop(banned, None)
    return out
