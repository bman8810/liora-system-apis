"""Unit tests for barge-in thrash reduction (no network)."""

from __future__ import annotations

import asyncio
from typing import List

import pytest

from voice_agent.turn_taking import BargeInAction, BargeInPolicy


class FakeClock:
    """Injectable monotonic clock for deterministic policy tests."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> float:
        self.t += seconds
        return self.t


def _policy(**kwargs) -> tuple[BargeInPolicy, FakeClock]:
    clock = FakeClock()
    defaults = dict(
        backchannel_hold_ms=450.0,
        barge_debounce_ms=350.0,
        min_response_commit_ms=600.0,
        tool_hold_barge=True,
        hard_barge_always=False,
        clock=clock,
    )
    defaults.update(kwargs)
    return BargeInPolicy(**defaults), clock


# ---------------------------------------------------------------------------
# 1. Short backchannel during speaking → no HARD_BARGE
# ---------------------------------------------------------------------------

def test_short_backchannel_during_speaking_no_hard_barge():
    p, clock = _policy()
    p.mark_response_started(now=clock.t)
    p.mark_assistant_audio(now=clock.t)

    action = p.on_speech_started(now=clock.t)
    assert action == BargeInAction.SOFT_HOLD
    assert p.hold_pending

    # Speech ends before hold elapses (e.g. "yeah")
    clock.advance(0.200)
    stopped = p.on_speech_stopped(now=clock.t)
    assert stopped == BargeInAction.IGNORE
    assert not p.hold_pending

    # Poll after nominal hold would have fired — still IGNORE
    clock.advance(0.300)
    assert p.poll(now=clock.t) == BargeInAction.IGNORE

    assert p.hard_barges == 0
    assert p.soft_holds == 1
    assert p.backchannels_ignored == 1


# ---------------------------------------------------------------------------
# 2. Sustained speech past hold → HARD_BARGE
# ---------------------------------------------------------------------------

def test_sustained_speech_past_hold_hard_barge():
    p, clock = _policy()
    p.mark_assistant_audio(now=clock.t)

    assert p.on_speech_started(now=clock.t) == BargeInAction.SOFT_HOLD
    # Hold is max(backchannel 450ms, remaining commit 600ms) at response start
    assert p.hold_remaining_s(now=clock.t) == pytest.approx(0.600, abs=0.001)

    clock.advance(0.450)
    assert p.poll(now=clock.t) == BargeInAction.SOFT_HOLD  # still in commit window

    clock.advance(0.200)  # past 600ms commit extension
    action = p.poll(now=clock.t)
    assert action == BargeInAction.HARD_BARGE
    assert p.hard_barges == 1
    assert p.backchannels_ignored == 0


# ---------------------------------------------------------------------------
# 3. Debounce suppresses second hard barge within window
# ---------------------------------------------------------------------------

def test_debounce_suppresses_second_hard_barge():
    p, clock = _policy(barge_debounce_ms=350.0)
    # Idle (no assistant) → first speech is hard barge
    a1 = p.on_speech_started(now=clock.t)
    assert a1 == BargeInAction.HARD_BARGE
    p.on_speech_stopped(now=clock.t)

    # Immediate second edge within debounce window
    clock.advance(0.100)
    a2 = p.on_speech_started(now=clock.t)
    assert a2 == BargeInAction.IGNORE
    assert p.debounced == 1
    assert p.hard_barges == 1

    # After debounce window, hard barge allowed again
    p.on_speech_stopped(now=clock.t)
    clock.advance(0.300)
    a3 = p.on_speech_started(now=clock.t)
    assert a3 == BargeInAction.HARD_BARGE
    assert p.hard_barges == 2


# ---------------------------------------------------------------------------
# 4. Tool inflight + short speech → no hard barge
# ---------------------------------------------------------------------------

def test_tool_inflight_short_speech_no_hard_barge():
    p, clock = _policy()
    p.mark_tool_inflight(True)
    # No assistant audio yet, but tool running
    action = p.on_speech_started(now=clock.t)
    assert action == BargeInAction.SOFT_HOLD

    clock.advance(0.100)
    assert p.on_speech_stopped(now=clock.t) == BargeInAction.IGNORE
    assert p.hard_barges == 0
    assert p.backchannels_ignored == 1


def test_tool_inflight_sustained_speech_can_hard_barge():
    """If user keeps talking past hold during tool call, allow interrupt."""
    p, clock = _policy()
    p.mark_tool_inflight(True)
    assert p.on_speech_started(now=clock.t) == BargeInAction.SOFT_HOLD
    clock.advance(0.450)
    assert p.poll(now=clock.t) == BargeInAction.HARD_BARGE
    assert p.hard_barges == 1


# ---------------------------------------------------------------------------
# 5. VOICE_HARD_BARGE_ALWAYS restores instant hard barge
# ---------------------------------------------------------------------------

def test_hard_barge_always_escape_hatch():
    p, clock = _policy(hard_barge_always=True)
    p.mark_assistant_audio(now=clock.t)
    action = p.on_speech_started(now=clock.t)
    assert action == BargeInAction.HARD_BARGE
    assert p.hard_barges == 1
    assert p.soft_holds == 0


# ---------------------------------------------------------------------------
# Commit window: early speech is soft, not instant hard
# ---------------------------------------------------------------------------

def test_commit_window_uses_soft_hold():
    p, clock = _policy(min_response_commit_ms=600.0, backchannel_hold_ms=450.0)
    p.mark_response_started(now=clock.t)
    # Immediately after response starts
    clock.advance(0.050)
    assert p.on_speech_started(now=clock.t) == BargeInAction.SOFT_HOLD
    # After commit window, still soft hold while speaking (not instant cancel)
    p.on_speech_stopped(now=clock.t)
    clock.advance(0.700)
    p.mark_assistant_audio(now=clock.t)
    assert p.on_speech_started(now=clock.t) == BargeInAction.SOFT_HOLD


# ---------------------------------------------------------------------------
# 6. Integration-style: AudioPipeline with fakes
# ---------------------------------------------------------------------------

class FakeMedia:
    def __init__(self):
        self.on_audio_received = None
        self.flushed = 0
        self.sent: List[bytes] = []

    async def start_sending(self):
        pass

    async def send_audio(self, mulaw_bytes: bytes):
        self.sent.append(mulaw_bytes)

    def flush_outbound(self):
        self.flushed += 1


class FakeBridge:
    def __init__(self):
        self.on_audio = None
        self.on_speech_started = None
        self.on_speech_stopped = None
        self.on_response_done = None
        self.on_transcript = None
        self.cancel_calls = 0
        self.truncate_calls = 0
        self._tool_inflight = False

    @property
    def tool_inflight(self) -> bool:
        return self._tool_inflight

    async def cancel_response(self):
        self.cancel_calls += 1

    async def truncate_audio(self):
        self.truncate_calls += 1

    async def send_audio(self, mulaw_bytes: bytes):
        pass

    async def send_response_create(self):
        pass


def test_pipeline_short_hold_does_not_cancel():
    async def _run():
        from voice_agent.audio_pipeline import AudioPipeline

        media = FakeMedia()
        bridge = FakeBridge()
        pipe = AudioPipeline(media, bridge)  # type: ignore[arg-type]
        pipe._running = True

        # Force short hold for fast test
        pipe._barge_policy = BargeInPolicy(
            backchannel_hold_ms=80.0,
            barge_debounce_ms=50.0,
            min_response_commit_ms=100.0,
            tool_hold_barge=True,
            hard_barge_always=False,
        )

        # Assistant is speaking
        await pipe._on_bridge_audio(b"\xff" * 160)

        await pipe._on_speech_started()
        assert bridge.cancel_calls == 0
        assert media.flushed == 0
        assert pipe._soft_hold_task is not None
        assert not pipe._interrupted

        # Backchannel ends quickly
        await pipe._on_speech_stopped()
        # Let cancelled task settle
        await asyncio.sleep(0)
        assert bridge.cancel_calls == 0
        assert media.flushed == 0
        assert pipe._soft_hold_task is None or pipe._soft_hold_task.done()

    asyncio.run(_run())


def test_pipeline_long_hold_does_cancel():
    async def _run():
        from voice_agent.audio_pipeline import AudioPipeline

        media = FakeMedia()
        bridge = FakeBridge()
        pipe = AudioPipeline(media, bridge)  # type: ignore[arg-type]
        pipe._running = True

        pipe._barge_policy = BargeInPolicy(
            backchannel_hold_ms=50.0,
            barge_debounce_ms=10.0,
            min_response_commit_ms=100.0,
            tool_hold_barge=True,
            hard_barge_always=False,
        )

        await pipe._on_bridge_audio(b"\xff" * 160)
        await pipe._on_speech_started()
        assert bridge.cancel_calls == 0

        # Wait past soft hold
        await asyncio.sleep(0.12)
        assert bridge.cancel_calls == 1
        assert bridge.truncate_calls == 1
        assert media.flushed == 1
        assert pipe._interrupted is True

    asyncio.run(_run())


def test_pipeline_hard_barge_always_cancels_immediately():
    async def _run():
        from voice_agent.audio_pipeline import AudioPipeline

        media = FakeMedia()
        bridge = FakeBridge()
        pipe = AudioPipeline(media, bridge)  # type: ignore[arg-type]
        pipe._running = True

        pipe._barge_policy = BargeInPolicy(
            backchannel_hold_ms=500.0,
            hard_barge_always=True,
        )

        await pipe._on_bridge_audio(b"\xff" * 160)
        await pipe._on_speech_started()
        assert bridge.cancel_calls == 1
        assert media.flushed == 1

    asyncio.run(_run())


def test_pipeline_tool_inflight_defers_cancel():
    async def _run():
        from voice_agent.audio_pipeline import AudioPipeline

        media = FakeMedia()
        bridge = FakeBridge()
        bridge._tool_inflight = True
        pipe = AudioPipeline(media, bridge)  # type: ignore[arg-type]
        pipe._running = True

        pipe._barge_policy = BargeInPolicy(
            backchannel_hold_ms=80.0,
            tool_hold_barge=True,
            hard_barge_always=False,
        )

        await pipe._on_speech_started()
        assert bridge.cancel_calls == 0
        await pipe._on_speech_stopped()
        await asyncio.sleep(0)
        assert bridge.cancel_calls == 0

    asyncio.run(_run())


def test_grok_bridge_tool_inflight_property():
    """tool_inflight tracks running tools and pending flush task."""
    from voice_agent.grok_bridge import GrokBridge

    # Avoid needing a real API key path beyond init
    bridge = GrokBridge.__new__(GrokBridge)
    bridge._tools_running = 0
    bridge._tool_flush_task = None
    assert bridge.tool_inflight is False

    bridge._tools_running = 1
    assert bridge.tool_inflight is True

    bridge._tools_running = 0

    class _Pending:
        def done(self):
            return False

    bridge._tool_flush_task = _Pending()  # type: ignore[assignment]
    assert bridge.tool_inflight is True

    class _Done:
        def done(self):
            return True

    bridge._tool_flush_task = _Done()  # type: ignore[assignment]
    assert bridge.tool_inflight is False
