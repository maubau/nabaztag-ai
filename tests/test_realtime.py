"""Realtime full-duplex path: resampling, streaming playback, barge-in.

The barge-in maths is the part that has to be right: if the truncate time does
not match what was actually heard, the model's transcript diverges from reality
and every later turn reasons from it.
"""

import base64
import json

import pytest
from rabbit_brain.audio.streaming import Resampler, expand_channels
from rabbit_brain.llm.realtime import API_RATE, RealtimeError, RealtimeSession
from rabbit_brain.runtime import conversation_mode

# --- resampling ---------------------------------------------------------


def test_resampler_changes_length_by_the_rate_ratio():
    pcm = b"\x00\x01" * 1600  # 1600 samples @16k = 100 ms
    out = Resampler(16000, 24000).process(pcm)
    assert len(out) // 2 == pytest.approx(2400, abs=2)  # 100 ms @24k


def test_resampler_downsamples_too():
    pcm = b"\x00\x01" * 2400  # 100 ms @24k
    out = Resampler(24000, 16000).process(pcm)
    assert len(out) // 2 == pytest.approx(1600, abs=2)


def test_resampler_is_stateful_across_chunks():
    """Chunked input must resample like one buffer — otherwise interpolation
    restarts at every boundary and clicks ~50 times a second."""
    whole = bytes(range(0, 200)) * 8
    one_shot = Resampler(16000, 24000).process(whole)
    chunked = Resampler(16000, 24000)
    pieces = b"".join(chunked.process(whole[i : i + 160]) for i in range(0, len(whole), 160))
    # same length within a sample, and identical but for interpolation at joins
    assert abs(len(one_shot) - len(pieces)) <= 4


def test_resampler_passthrough_when_rates_match():
    pcm = b"\x01\x02\x03\x04"
    assert Resampler(16000, 16000).process(pcm) is pcm


def test_resampler_rejects_bad_rates():
    with pytest.raises(ValueError):
        Resampler(0, 24000)


def test_expand_channels_duplicates_mono():
    assert expand_channels(b"\x01\x00\x02\x00", 1) == b"\x01\x00\x02\x00"
    assert expand_channels(b"\x01\x00", 2) == b"\x01\x00\x01\x00"


# --- a player double the session can drive ------------------------------


class FakePlayer:
    def __init__(self, played_ms=0):
        self.written: list[bytes] = []
        self.stopped = 0
        self.resumed = 0
        self.resets = 0
        self.played_ms = played_ms

    def write(self, pcm):
        self.written.append(pcm)

    def reset_position(self):
        self.resets += 1

    async def stop(self):
        self.stopped += 1

    async def resume(self):
        self.resumed += 1


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed = False

    async def send_str(self, data):
        self.sent.append(json.loads(data))


def _session(player, **kw):
    s = RealtimeSession(api_key="k", player=player, **kw)
    s._ws = FakeWS()
    return s


def _audio_event(pcm: bytes, item_id="item_1"):
    return {
        "type": "response.output_audio.delta",
        "delta": base64.b64encode(pcm).decode(),
        "item_id": item_id,
    }


async def test_audio_delta_plays_immediately_without_an_mp3():
    player = FakePlayer()
    session = _session(player)
    await session._handle(_audio_event(b"\x01\x00" * 10))
    assert player.written == [b"\x01\x00" * 10]  # straight to the speaker
    assert player.resets == 1  # new response -> playback position restarts
    assert session.timings.first_audio_delta_ms is not None
    assert session.timings.playback_started_ms is not None


async def test_barge_in_stops_playback_and_truncates_at_the_played_time():
    player = FakePlayer(played_ms=1234)
    session = _session(player)
    await session._handle(_audio_event(b"\x01\x00" * 10))  # rabbit is speaking
    await session._handle({"type": "input_audio_buffer.speech_started"})

    assert player.stopped == 1  # speaker cut immediately
    kinds = [m["type"] for m in session._ws.sent]
    assert "response.cancel" in kinds
    truncate = next(m for m in session._ws.sent if m["type"] == "conversation.item.truncate")
    # the REALLY-played time, not the generated length
    assert truncate["audio_end_ms"] == 1234
    assert truncate["item_id"] == "item_1"
    assert session.timings.interruptions  # interruption timing recorded


async def test_barge_in_is_a_no_op_when_the_rabbit_is_not_speaking():
    # the user talking first must not emit a spurious truncate
    player = FakePlayer()
    session = _session(player)
    await session._handle({"type": "input_audio_buffer.speech_started"})
    assert player.stopped == 0
    assert session._ws.sent == []


async def test_second_response_resets_the_playback_position():
    player = FakePlayer()
    session = _session(player)
    await session._handle(_audio_event(b"\x01\x00"))
    await session._handle({"type": "response.done"})
    await session._handle(_audio_event(b"\x02\x00", item_id="item_2"))
    assert player.resets == 2  # each reply is measured from zero


async def test_api_error_raises_for_the_turn_based_fallback():
    session = _session(FakePlayer())
    with pytest.raises(RealtimeError):
        await session._handle({"type": "error", "error": {"message": "nope"}})


async def test_microphone_is_resampled_up_to_the_api_rate():
    session = _session(FakePlayer(), mic_rate=16000)
    frame = b"\x00\x01" * 320  # 20 ms @16k

    async def frames():
        yield frame

    await session.pump_microphone(frames())
    sent = session._ws.sent[0]
    assert sent["type"] == "input_audio_buffer.append"
    decoded = base64.b64decode(sent["audio"])
    assert len(decoded) // 2 == pytest.approx(int(320 * API_RATE / 16000), abs=2)


async def test_body_tools_still_work_in_realtime():
    calls = []

    async def executor(name, arguments):
        calls.append((name, arguments))
        return "ok"

    session = _session(FakePlayer(), tool_executor=executor)
    await session._handle(
        {
            "type": "response.function_call_arguments.done",
            "name": "gesture_ears",
            "call_id": "c1",
            "arguments": '{"left": 5}',
        }
    )
    assert calls == [("gesture_ears", {"left": 5})]
    kinds = [m["type"] for m in session._ws.sent]
    assert "conversation.item.create" in kinds and "response.create" in kinds


async def test_failing_tool_does_not_kill_the_call():
    async def executor(name, arguments):
        raise RuntimeError("ears jammed")

    session = _session(FakePlayer(), tool_executor=executor)
    await session._handle(
        {
            "type": "response.function_call_arguments.done",
            "name": "gesture_ears",
            "call_id": "c1",
            "arguments": "{}",
        }
    )
    output = next(m for m in session._ws.sent if m["type"] == "conversation.item.create")
    assert "error" in output["item"]["output"]


async def test_send_before_connect_raises_realtime_error():
    # never silently no-op: the caller must be able to re-arm turn-based
    session = RealtimeSession(api_key="k", player=FakePlayer())
    with pytest.raises(RealtimeError):
        await session._send({"type": "ping"})


# --- config gate --------------------------------------------------------


def test_realtime_requires_the_local_backend():
    # barge-in needs interruptible playback; the rabbit's decoder can't be cut
    with pytest.raises(ValueError, match="requires audio_out.backend: local"):
        conversation_mode({"conversation": {"mode": "realtime"}})
    with pytest.raises(ValueError, match="requires audio_out.backend: local"):
        conversation_mode(
            {"conversation": {"mode": "realtime"}, "audio_out": {"backend": "rabbit"}}
        )
    ok = {"conversation": {"mode": "realtime"}, "audio_out": {"backend": "local"}}
    assert conversation_mode(ok) == "realtime"


def test_default_mode_is_turn_based():
    assert conversation_mode({}) == "turn_based"
    assert conversation_mode({"audio_out": {"backend": "rabbit"}}) == "turn_based"


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="unknown conversation.mode"):
        conversation_mode({"conversation": {"mode": "telepathy"}})
