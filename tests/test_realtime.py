"""Realtime full-duplex path: resampling, streaming playback, barge-in.

The barge-in maths is the part that has to be right: if the truncate time does
not match what was actually heard, the model's transcript diverges from reality
and every later turn reasons from it.
"""

import asyncio
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
    # 1250 ms of 24 kHz mono: the item must really contain what we claim was heard
    await session._handle(_audio_event(b"\x01\x00" * 30000))
    await session._handle({"type": "input_audio_buffer.speech_started"})

    assert player.stopped == 1  # speaker cut immediately
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


async def test_fatal_api_error_raises_for_the_turn_based_fallback():
    # only errors that really make the session unusable bring it down
    session = _session(FakePlayer())
    with pytest.raises(RealtimeError):
        await session._handle(
            {"type": "error", "error": {"code": "session_expired", "message": "nope"}}
        )


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


# --- hardware-found races (July 2026) -----------------------------------


async def test_speech_started_does_not_cancel_under_server_side_vad():
    """With VAD the server cancels the response itself; our extra
    response.cancel raced it, came back response_cancel_not_active, and killed
    the whole session on every SUCCESSFUL barge-in."""
    session = _session(FakePlayer(played_ms=500), turn_detection="semantic_vad")
    await session._handle(_audio_event(b"\x01\x00" * 24000))  # 1000 ms
    await session._handle({"type": "input_audio_buffer.speech_started"})
    kinds = [m["type"] for m in session._ws.sent]
    assert "response.cancel" not in kinds  # the server already did it
    assert "conversation.item.truncate" in kinds  # but we still report what was heard


async def test_cancel_not_active_error_does_not_end_the_session():
    session = _session(FakePlayer())
    # must NOT raise: this is a benign race, not a failure
    await session._handle(
        {
            "type": "error",
            "error": {"code": "response_cancel_not_active", "message": "no active response found"},
        }
    )


async def test_unknown_errors_are_logged_but_not_fatal():
    session = _session(FakePlayer())
    await session._handle({"type": "error", "error": {"code": "weird_thing", "message": "hm"}})


async def test_auth_errors_are_still_fatal():
    session = _session(FakePlayer())
    with pytest.raises(RealtimeError):
        await session._handle(
            {"type": "error", "error": {"code": "invalid_api_key", "message": "bad key"}}
        )


async def test_barge_in_before_any_audio_played_sends_no_truncate():
    """played_ms == 0: nothing was heard, so there is nothing to trim — and
    truncating at 0 only invites an invalid-value error."""
    session = _session(FakePlayer(played_ms=0))
    await session._handle(_audio_event(b"\x01\x00"))
    await session._handle({"type": "input_audio_buffer.speech_started"})
    kinds = [m["type"] for m in session._ws.sent]
    assert "conversation.item.truncate" not in kinds
    assert session.timings.interruptions  # still recorded as an interruption


# --- callback-driven player (no blockable Python thread) -----------------


class FakeCallbackStream:
    """Stands in for a PortAudio callback stream: the test pumps the callback
    itself, exactly as the driver's own thread would."""

    def __init__(self, channels=1, fail_close=False):
        self.callback = None
        self.channels = channels
        self.latency = 0.02
        self.started = 0
        self.stopped = 0
        self.closed = 0
        self.fail_close = fail_close

    def start(self):
        self.started += 1

    def stop(self):
        self.stopped += 1

    def close(self):
        if self.fail_close:
            raise RuntimeError("device busy")
        self.closed += 1

    def pump(self, frames):
        """Run one callback and return what the device would have played."""
        buf = bytearray(frames * 2 * self.channels)
        self.callback(buf, frames, None, None)
        return bytes(buf)


async def _callback_player(stream=None, rate=24000):
    from rabbit_brain.audio.streaming import StreamingAudioPlayer

    stream = stream or FakeCallbackStream()
    player = StreamingAudioPlayer(device=None, device_rate=rate, device_channels=1)

    def _open():
        stream.callback = player._callback
        return stream

    player._open_stream = _open
    await player.start()
    return player, stream


async def test_callback_plays_queued_pcm_and_counts_real_frames():
    player, stream = await _callback_player()
    player.write(b"\x01\x00" * 100)
    played = stream.pump(100)
    assert played == b"\x01\x00" * 100  # the device really got it
    # played_ms is a COUNT now, not an estimate (minus the device buffer)
    assert player.played_ms == max(0, int(100 / 24000 * 1000 - 20))


async def test_callback_outputs_silence_when_the_buffer_is_empty():
    player, stream = await _callback_player()
    assert stream.pump(50) == b"\x00" * 100  # silence, never a stall


async def test_stop_makes_the_very_next_callback_silent():
    """Barge-in needs no abort(): dropping the buffer is enough, and the stream
    stays healthy for the next reply instead of being torn down."""
    player, stream = await _callback_player()
    player.write(b"\x01\x00" * 500)
    stream.pump(10)  # started playing
    await player.stop()
    assert stream.pump(50) == b"\x00" * 100  # cut immediately
    assert stream.stopped == 0, "the stream must NOT be torn down to interrupt"


async def test_interrupted_audio_never_bleeds_into_the_next_response():
    player, stream = await _callback_player()
    player.write(b"\x01\x00" * 500)  # reply 1
    await player.stop()
    player.reset_position()  # reply 2 begins
    player.write(b"\x02\x00" * 10)
    played = stream.pump(10)
    assert played == b"\x02\x00" * 10, "audio from the interrupted reply leaked"


async def test_played_ms_is_not_inflated_by_a_previous_response():
    """The 'audio content is already shorter than' bug: a straggling chunk from
    the previous reply must not be credited to the new one."""
    player, stream = await _callback_player()
    player.write(b"\x01\x00" * 24000)  # a long reply 1
    stream.pump(12000)
    assert player.played_ms > 0
    player.reset_position()  # reply 2
    assert player.played_ms == 0, "the counter carried over between responses"
    stream.pump(1000)  # nothing queued for reply 2 yet -> silence, no credit
    assert player.played_ms == 0


async def test_has_unplayed_audio_tracks_buffer_and_device_window():
    player, stream = await _callback_player()
    assert player.has_unplayed_audio is False
    player.write(b"\x01\x00" * 500)
    assert player.has_unplayed_audio is True  # buffered
    stream.pump(500)
    assert player.has_unplayed_audio is True  # still sounding (device latency)
    await player.stop()
    assert player.has_unplayed_audio is False


async def test_aclose_releases_the_device():
    player, stream = await _callback_player()
    await player.aclose()
    assert stream.stopped == 1 and stream.closed == 1


async def test_aclose_raises_when_the_device_cannot_be_reclaimed():
    """A device we cannot release must NOT look like a healthy shutdown: the
    next wake would light the LEDs and answer in silence."""
    from rabbit_brain.audio.streaming import AudioBackendUnrecoverable

    player, _ = await _callback_player(FakeCallbackStream(fail_close=True))
    with pytest.raises(AudioBackendUnrecoverable):
        await player.aclose()
    assert player.backend_broken is True


async def test_truncate_never_exceeds_the_items_own_duration():
    """The server rejects an over-long truncate outright, and then nothing is
    trimmed at all."""
    player = FakePlayer(played_ms=13804)  # counter says more than we ever sent
    session = _session(player)
    await session._handle(_audio_event(b"\x01\x00" * 12000))  # only 500 ms
    await session._handle({"type": "input_audio_buffer.speech_started"})
    truncate = next(m for m in session._ws.sent if m["type"] == "conversation.item.truncate")
    assert truncate["audio_end_ms"] == 500


async def test_output_item_added_does_not_defeat_the_per_item_reset():
    """`response.output_item.added` announces an item BEFORE any of its audio
    exists. Keying the reset on that id meant the ids already matched when the
    first delta arrived, so the counter simply ran on across items — hardware
    kept logging truncate 13968 ms against an 11250 ms item."""
    player = FakePlayer(played_ms=99999)
    session = _session(player)
    await session._handle(
        {"type": "response.output_item.added", "item": {"type": "message", "id": "item_1"}}
    )
    await session._handle(_audio_event(b"\x01\x00" * 12000, item_id="item_1"))  # 500 ms
    assert player.resets == 1, "the announced item suppressed the playback reset"

    # a sibling item in the SAME response: announced first, then its audio
    await session._handle(
        {"type": "response.output_item.added", "item": {"type": "message", "id": "item_2"}}
    )
    await session._handle(_audio_event(b"\x01\x00" * 4800, item_id="item_2"))  # 200 ms
    assert player.resets == 2

    await session._handle({"type": "input_audio_buffer.speech_started"})
    truncate = next(m for m in session._ws.sent if m["type"] == "conversation.item.truncate")
    # scoped to the item whose audio is playing, and capped by ITS duration
    assert truncate["item_id"] == "item_2"
    assert truncate["audio_end_ms"] == 200


# --- playback lifecycle events (what drives ASSISTANT_SPEAKING) ----------


async def _event_player(rate=24000):
    from rabbit_brain.audio.streaming import StreamingAudioPlayer

    seen: list[str] = []
    stream = FakeCallbackStream()
    player = StreamingAudioPlayer(
        device=None,
        device_rate=rate,
        device_channels=1,
        on_playback_started=lambda: seen.append("started"),
        on_playback_drained=lambda: seen.append("drained"),
        on_playback_cut=lambda: seen.append("cut"),
    )

    def _open():
        stream.callback = player._callback
        return stream

    player._open_stream = _open
    await player.start()
    return player, stream, seen


async def test_playback_events_follow_the_speaker_not_the_server():
    player, stream, seen = await _event_player()
    assert seen == []  # nothing queued: the rabbit is not talking
    player.write(b"\x01\x00" * 100)
    stream.pump(100)
    await asyncio.sleep(0)  # events hop through the loop
    assert seen == ["started"] and player.is_playing


async def test_a_gap_between_deltas_does_not_end_the_utterance():
    """Deltas arrive over the network and an item boundary empties the buffer for
    a moment: a bare underrun must not flicker the speaking indicator off."""
    player, stream, seen = await _event_player()
    player.write(b"\x01\x00" * 100)
    stream.pump(100)
    stream.pump(100)  # dry, but only just
    await asyncio.sleep(0)
    assert seen == ["started"], "a momentary underrun was mistaken for the end"
    assert player.is_playing


async def test_sustained_silence_reports_the_playback_drained():
    player, stream, seen = await _event_player()
    player.write(b"\x01\x00" * 100)
    stream.pump(100)
    stream.pump(100)  # runs dry: starts the debounce
    await asyncio.sleep(0.3)  # longer than the drain window
    stream.pump(100)
    await asyncio.sleep(0)
    assert seen == ["started", "drained"]
    assert not player.is_playing


async def test_stop_reports_a_cut_at_once_not_a_drain():
    """A barge-in must reach the UX immediately — not when the (already emptied)
    buffer would eventually have been noticed as dry."""
    player, stream, seen = await _event_player()
    player.write(b"\x01\x00" * 500)
    stream.pump(10)
    await player.stop()
    await asyncio.sleep(0)
    assert seen == ["started", "cut"]
    assert not player.is_playing


async def test_a_new_item_inside_one_reply_is_not_a_drain():
    player, stream, seen = await _event_player()
    player.write(b"\x01\x00" * 100)
    stream.pump(100)
    player.reset_position()  # next item of the same reply
    player.write(b"\x02\x00" * 100)
    stream.pump(100)
    await asyncio.sleep(0)
    assert seen == ["started"], "the indicator flapped at an item boundary"
