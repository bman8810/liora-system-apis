"""Barge-in / turn-taking policy for multi-intent voice calls.

Problem: always hard-canceling on every speech_started thrashs multi-intent
calls — short affirmations ("yeah", "ok") and overlapping second intents
restart the assistant mid-booking. This module decides IGNORE / SOFT_HOLD /
HARD_BARGE from pure time-based state so the audio pipeline can finish the
primary response when speech is only a backchannel, while still allowing
real interrupts after a short hold.

Env knobs (defaults in parentheses):
  VOICE_BACKCHANNEL_HOLD_MS (450)
  VOICE_BARGE_DEBOUNCE_MS (350)
  VOICE_MIN_RESPONSE_COMMIT_MS (600)
  VOICE_TOOL_HOLD_BARGE (true)
  VOICE_HARD_BARGE_ALWAYS (false)
"""

from __future__ import annotations

import os
import time
from enum import Enum
from typing import Callable, Optional


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class BargeInAction(str, Enum):
    """What the pipeline should do for a speech / poll event."""

    IGNORE = "ignore"
    SOFT_HOLD = "soft_hold"
    HARD_BARGE = "hard_barge"


class BargeInPolicy:
    """Decide whether user speech should hard-interrupt the assistant.

    Sync and pure time-based: pass ``now`` (seconds, monotonic) or inject
    ``clock`` so unit tests need no asyncio sleep.
    """

    def __init__(
        self,
        *,
        backchannel_hold_ms: Optional[float] = None,
        barge_debounce_ms: Optional[float] = None,
        min_response_commit_ms: Optional[float] = None,
        tool_hold_barge: Optional[bool] = None,
        hard_barge_always: Optional[bool] = None,
        clock: Optional[Callable[[], float]] = None,
    ):
        self.backchannel_hold_ms = (
            backchannel_hold_ms
            if backchannel_hold_ms is not None
            else _env_float("VOICE_BACKCHANNEL_HOLD_MS", 450.0)
        )
        self.barge_debounce_ms = (
            barge_debounce_ms
            if barge_debounce_ms is not None
            else _env_float("VOICE_BARGE_DEBOUNCE_MS", 350.0)
        )
        self.min_response_commit_ms = (
            min_response_commit_ms
            if min_response_commit_ms is not None
            else _env_float("VOICE_MIN_RESPONSE_COMMIT_MS", 600.0)
        )
        self.tool_hold_barge = (
            tool_hold_barge
            if tool_hold_barge is not None
            else _env_bool("VOICE_TOOL_HOLD_BARGE", True)
        )
        self.hard_barge_always = (
            hard_barge_always
            if hard_barge_always is not None
            else _env_bool("VOICE_HARD_BARGE_ALWAYS", False)
        )
        self._clock = clock or time.monotonic

        # Assistant / tool state
        self._response_started_at: Optional[float] = None
        self._assistant_speaking = False
        self._tool_inflight = False

        # User speech / hold state
        self._user_speaking = False
        self._hold_until: Optional[float] = None
        self._last_hard_barge_at: Optional[float] = None

        # Stats
        self.hard_barges = 0
        self.soft_holds = 0
        self.backchannels_ignored = 0
        self.debounced = 0

    # --- clock / helpers -------------------------------------------------

    def _now(self, now: Optional[float] = None) -> float:
        return self._clock() if now is None else now

    def _ms_to_s(self, ms: float) -> float:
        return ms / 1000.0

    def _assistant_active(self) -> bool:
        return self._assistant_speaking or self._tool_inflight or (
            self._response_started_at is not None
        )

    def _debounced(self, now: float) -> bool:
        if self._last_hard_barge_at is None:
            return False
        return (now - self._last_hard_barge_at) < self._ms_to_s(
            self.barge_debounce_ms
        )

    def _emit_hard(self, now: float) -> BargeInAction:
        if self._debounced(now):
            self.debounced += 1
            return BargeInAction.IGNORE
        self.hard_barges += 1
        self._last_hard_barge_at = now
        self._hold_until = None
        return BargeInAction.HARD_BARGE

    def _hold_duration_s(self, now: float) -> float:
        """Backchannel hold, extended through remaining response-commit window."""
        hold = self._ms_to_s(self.backchannel_hold_ms)
        if self._response_started_at is not None:
            elapsed = now - self._response_started_at
            commit = self._ms_to_s(self.min_response_commit_ms)
            if elapsed < commit:
                # Prefer finishing the first ~commit_ms of a response
                hold = max(hold, commit - elapsed)
        return hold

    def _start_hold(self, now: float) -> BargeInAction:
        # Refresh hold deadline if already holding
        self._hold_until = now + self._hold_duration_s(now)
        self.soft_holds += 1
        return BargeInAction.SOFT_HOLD

    # --- state markers ---------------------------------------------------

    def mark_response_started(self, now: Optional[float] = None) -> None:
        t = self._now(now)
        self._response_started_at = t
        self._assistant_speaking = True

    def mark_response_done(self, now: Optional[float] = None) -> None:
        self._response_started_at = None
        self._assistant_speaking = False

    def mark_assistant_audio(self, now: Optional[float] = None) -> None:
        """Outbound assistant audio is flowing — treat as speaking."""
        t = self._now(now)
        self._assistant_speaking = True
        if self._response_started_at is None:
            self._response_started_at = t

    def mark_tool_inflight(self, inflight: bool) -> None:
        self._tool_inflight = bool(inflight)

    # --- event handlers --------------------------------------------------

    def on_speech_started(self, now: Optional[float] = None) -> BargeInAction:
        t = self._now(now)
        self._user_speaking = True

        # Escape hatch: restore always-cancel behavior
        if self.hard_barge_always:
            return self._emit_hard(t)

        # Nothing in flight — allow hard barge (clear residual buffer / idle)
        if not self._assistant_active():
            return self._emit_hard(t)

        # Tool in flight: never instant hard-cancel; only start hold
        if self._tool_inflight and self.tool_hold_barge:
            return self._start_hold(t)

        # Assistant speaking / response commit: soft hold (backchannel filter).
        # Commit window and post-commit both use hold — never instant cancel
        # while the primary response is still active.
        if self._assistant_speaking or self._response_started_at is not None:
            return self._start_hold(t)

        return self._emit_hard(t)

    def on_speech_stopped(self, now: Optional[float] = None) -> BargeInAction:
        """If a soft hold is pending and speech ends early → backchannel."""
        t = self._now(now)
        self._user_speaking = False

        if self._hold_until is not None:
            # Stopped before (or while) hold — treat as backchannel
            self._hold_until = None
            self.backchannels_ignored += 1
            return BargeInAction.IGNORE

        return BargeInAction.IGNORE

    def poll(self, now: Optional[float] = None) -> BargeInAction:
        """If soft hold elapsed while user still speaking → HARD_BARGE."""
        t = self._now(now)
        if self._hold_until is None:
            return BargeInAction.IGNORE

        if t < self._hold_until:
            return BargeInAction.SOFT_HOLD  # still waiting

        # Hold elapsed
        self._hold_until = None
        if not self._user_speaking:
            # Speech already ended; backchannel path should have cleared this,
            # but be defensive.
            self.backchannels_ignored += 1
            return BargeInAction.IGNORE

        return self._emit_hard(t)

    # --- introspection (tests / logging) ---------------------------------

    @property
    def hold_pending(self) -> bool:
        return self._hold_until is not None

    def hold_remaining_s(self, now: Optional[float] = None) -> Optional[float]:
        """Seconds until soft hold expires, or None if no hold pending."""
        if self._hold_until is None:
            return None
        t = self._now(now)
        return max(0.0, self._hold_until - t)

    @property
    def user_speaking(self) -> bool:
        return self._user_speaking

    @property
    def assistant_speaking(self) -> bool:
        return self._assistant_speaking

    @property
    def response_in_flight(self) -> bool:
        return self._response_started_at is not None

    @property
    def tool_inflight(self) -> bool:
        return self._tool_inflight
